# piker: trading gear for hackers
# Copyright (C) Tyler Goodlet (in stewardship for pikers)

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Order api and machinery

'''
from contextlib import asynccontextmanager as acm
from functools import partial
from itertools import chain
from pprint import pformat
import time
from typing import (
    Any,
    AsyncIterator,
    # Callable,
    # Optional,
    # Union,
)

import pendulum
import trio
import tractor
import wsproto

from piker import pp
from piker.clearing._messages import (
    BrokerdCancel,
    BrokerdError,
    BrokerdFill,
    BrokerdOrder,
    BrokerdOrderAck,
    BrokerdPosition,
    BrokerdStatus,
)
from piker.data.types import Struct
from . import log
from .api import (
    Client,
    BrokerError,
    get_client,
    normalize_symbol,
)
from .feed import (
    get_console_log,
    open_autorecon_ws,
    NoBsWs,
    stream_messages,
)


class Trade(Struct):
    '''
    Trade class that helps parse and validate ownTrades stream

    '''
    reqid: str  # kraken order transaction id
    action: str  # buy or sell
    price: float  # price of asset
    size: float  # vol of asset
    broker_time: str  # e.g GTC, GTD


async def handle_order_requests(

    client: Client,
    ems_order_stream: tractor.MsgStream,

) -> None:

    request_msg: dict
    order: BrokerdOrder

    async for request_msg in ems_order_stream:
        log.info(
            'Received order request:\n'
            f'{pformat(request_msg)}'
        )

        action = request_msg['action']

        if action in {'buy', 'sell'}:

            account = request_msg['account']
            if account != 'kraken.spot':
                log.error(
                    'This is a kraken account, \
                    only a `kraken.spot` selection is valid'
                )
                await ems_order_stream.send(BrokerdError(
                    oid=request_msg['oid'],
                    symbol=request_msg['symbol'],

                    # reason=f'Kraken only, No account found: `{account}` ?',
                    reason=(
                        'Kraken only, order mode disabled due to '
                        'https://github.com/pikers/piker/issues/299'
                    ),

                ))
                continue

            # validate
            order = BrokerdOrder(**request_msg)
            # call our client api to submit the order
            resp = await client.submit_limit(
                symbol=order.symbol,
                price=order.price,
                action=order.action,
                size=order.size,
                reqid=order.reqid,
            )

            err = resp['error']
            if err:
                oid = order.oid
                log.error(f'Failed to submit order: {oid}')

                await ems_order_stream.send(
                    BrokerdError(
                        oid=order.oid,
                        reqid=order.reqid,
                        symbol=order.symbol,
                        reason="Failed order submission",
                        broker_details=resp
                    )
                )
            else:
                # TODO: handle multiple orders (cancels?)
                #       txid is an array of strings
                if order.reqid is None:
                    reqid = resp['result']['txid'][0]
                else:
                    # update the internal pairing of oid to krakens
                    # txid with the new txid that is returned on edit
                    reqid = resp['result']['txid']

                # deliver ack that order has been submitted to broker routing
                await ems_order_stream.send(
                    BrokerdOrderAck(

                        # ems order request id
                        oid=order.oid,

                        # broker specific request id
                        reqid=reqid,

                        # account the made the order
                        account=order.account

                    )
                )

        elif action == 'cancel':
            msg = BrokerdCancel(**request_msg)

            # Send order cancellation to kraken
            resp = await client.submit_cancel(
                reqid=msg.reqid
            )

            # Check to make sure there was no error returned by
            # the kraken endpoint. Assert one order was cancelled.
            try:
                result = resp['result']
                count = result['count']

            # check for 'error' key if we received no 'result'
            except KeyError:
                error = resp.get('error')

                await ems_order_stream.send(
                    BrokerdError(
                        oid=msg.oid,
                        reqid=msg.reqid,
                        symbol=msg.symbol,
                        reason="Failed order cancel",
                        broker_details=resp
                    )
                )

                if not error:
                    raise BrokerError(f'Unknown order cancel response: {resp}')

            else:
                if not count:  # no orders were cancelled?

                    # XXX: what exactly is this from and why would we care?
                    # there doesn't seem to be any docs here?
                    # https://docs.kraken.com/rest/#operation/cancelOrder

                    # Check to make sure the cancellation is NOT pending,
                    # then send the confirmation to the ems order stream
                    pending = result.get('pending')
                    if pending:
                        log.error(f'Order {oid} cancel was not yet successful')

                        await ems_order_stream.send(
                            BrokerdError(
                                oid=msg.oid,
                                reqid=msg.reqid,
                                symbol=msg.symbol,
                                # TODO: maybe figure out if pending
                                # cancels will eventually get cancelled
                                reason="Order cancel is still pending?",
                                broker_details=resp
                            )
                        )

                else:  # order cancel success case.

                    await ems_order_stream.send(
                        BrokerdStatus(
                            reqid=msg.reqid,
                            account=msg.account,
                            time_ns=time.time_ns(),
                            status='cancelled',
                            reason='Order cancelled',
                            broker_details={'name': 'kraken'}
                        )
                    )
    else:
        log.error(f'Unknown order command: {request_msg}')


@acm
async def subscribe(
    ws: wsproto.WSConnection,
    token: str,
    subs: list[str] = ['ownTrades', 'openOrders'],
):
    '''
    Setup ws api subscriptions:
    https://docs.kraken.com/websockets/#message-subscribe

    By default we sign up for trade and order update events.

    '''
    # more specific logic for this in kraken's sync client:
    # https://github.com/krakenfx/kraken-wsclient-py/blob/master/kraken_wsclient_py/kraken_wsclient_py.py#L188

    assert token
    for sub in subs:
        msg = {
            'event': 'subscribe',
            'subscription': {
                'name': sub,
                'token': token,
            }
        }

        # TODO: we want to eventually allow unsubs which should
        # be completely fine to request from a separate task
        # since internally the ws methods appear to be FIFO
        # locked.
        await ws.send_msg(msg)

    yield

    for sub in subs:
        # unsub from all pairs on teardown
        await ws.send_msg({
            'event': 'unsubscribe',
            'subscription': [sub],
        })

    # XXX: do we need to ack the unsub?
    # await ws.recv_msg()


@tractor.context
async def trades_dialogue(
    ctx: tractor.Context,
    loglevel: str = None,
) -> AsyncIterator[dict[str, Any]]:

    # XXX: required to propagate ``tractor`` loglevel to piker logging
    get_console_log(loglevel or tractor.current_actor().loglevel)

    async with get_client() as client:

        # TODO: make ems flip to paper mode via
        # some returned signal if the user only wants to use
        # the data feed or we return this?
        # await ctx.started(({}, ['paper']))

        if not client._api_key:
            raise RuntimeError(
                'Missing Kraken API key in `brokers.toml`!?!?')

        # auth required block
        acctid = client._name
        acc_name = 'kraken.' + acctid

        # pull and deliver trades ledger
        trades = await client.get_trades()
        log.info(
            f'Loaded {len(trades)} trades from account `{acc_name}`'
        )
        trans = await update_ledger(acctid, trades)
        active, closed = pp.update_pps_conf(
            'kraken',
            acctid,
            trade_records=trans,
            ledger_reload={}.fromkeys(t.bsuid for t in trans),
        )

        position_msgs: list[dict] = []
        pps: dict[int, pp.Position]
        for pps in [active, closed]:
            for tid, p in pps.items():
                msg = BrokerdPosition(
                    broker='kraken',
                    account=acc_name,
                    symbol=p.symbol.front_fqsn(),
                    size=p.size,
                    avg_price=p.be_price,
                    currency='',
                )
                position_msgs.append(msg)

        await ctx.started(
            (position_msgs, [acc_name])
        )

        # Get websocket token for authenticated data stream
        # Assert that a token was actually received.
        resp = await client.endpoint('GetWebSocketsToken', {})

        err = resp.get('error')
        if err:
            raise BrokerError(err)

        token = resp['result']['token']

        ws: NoBsWs
        async with (
            ctx.open_stream() as ems_stream,
            open_autorecon_ws(
                'wss://ws-auth.kraken.com/',
                fixture=partial(
                    subscribe,
                    token=token,
                ),
            ) as ws,
            trio.open_nursery() as n,
        ):
            # task for processing inbound requests from ems
            n.start_soon(handle_order_requests, client, ems_stream)

            count: int = 0

            # process and relay trades events to ems
            # https://docs.kraken.com/websockets/#message-ownTrades
            async for msg in stream_messages(ws):
                match msg:
                    case [
                        trades_msgs,
                        'ownTrades',
                        {'sequence': seq},
                    ]:
                        # XXX: do we actually need this orrr?
                        # ensure that we are only processing new trades?
                        assert seq > count
                        count += 1

                        # flatten msgs for processing
                        trades = {
                            tid: trade
                            for entry in trades_msgs
                            for (tid, trade) in entry.items()

                            # only emit entries which are already not-in-ledger
                            if tid not in {r.tid for r in trans}
                        }
                        for tid, trade in trades.items():

                            # parse-cast
                            reqid = trade['ordertxid']
                            action = trade['type']
                            price = float(trade['price'])
                            size = float(trade['vol'])
                            broker_time = float(trade['time'])

                            # send a fill msg for gui update
                            fill_msg = BrokerdFill(
                                reqid=reqid,
                                time_ns=time.time_ns(),

                                action=action,
                                size=size,
                                price=price,
                                # TODO: maybe capture more msg data
                                # i.e fees?
                                broker_details={'name': 'kraken'},
                                broker_time=broker_time
                            )
                            await ems_stream.send(fill_msg)

                            filled_msg = BrokerdStatus(
                                reqid=reqid,
                                time_ns=time.time_ns(),

                                account=acc_name,
                                status='filled',
                                filled=size,
                                reason='Order filled by kraken',
                                broker_details={
                                    'name': 'kraken',
                                    'broker_time': broker_time
                                },

                                # TODO: figure out if kraken gives a count
                                # of how many units of underlying were
                                # filled. Alternatively we can decrement
                                # this value ourselves by associating and
                                # calcing from the diff with the original
                                # client-side request, see:
                                # https://github.com/pikers/piker/issues/296
                                remaining=0,
                            )
                            await ems_stream.send(filled_msg)

                        # update ledger and position tracking
                        trans = await update_ledger(acctid, trades)
                        active, closed = pp.update_pps_conf(
                            'kraken',
                            acctid,
                            trade_records=trans,
                            ledger_reload={}.fromkeys(
                                t.bsuid for t in trans),
                        )

                        # emit pp msgs
                        for pos in filter(
                            bool,
                            chain(active.values(), closed.values()),
                        ):
                            pp_msg = BrokerdPosition(
                                broker='kraken',

                                # XXX: ok so this is annoying, we're
                                # relaying an account name with the
                                # backend suffix prefixed but when
                                # reading accounts from ledgers we
                                # don't need it and/or it's prefixed
                                # in the section table.. we should
                                # just strip this from the message
                                # right since `.broker` is already
                                # included?
                                account=f'kraken.{acctid}',
                                symbol=pos.symbol.front_fqsn(),
                                size=pos.size,
                                avg_price=pos.be_price,

                                # TODO
                                # currency=''
                            )
                            await ems_stream.send(pp_msg)

                    case [
                        trades_msgs,
                        'openOrders',
                        {'sequence': seq},
                    ]:
                        # TODO: async order update handling which we
                        # should remove from `handle_order_requests()`
                        # above:
                        # https://github.com/pikers/piker/issues/293
                        # https://github.com/pikers/piker/issues/310
                        log.info(f'Order update {seq}:{trades_msgs}')

                    case _:
                        log.warning(f'Unhandled trades msg: {msg}')
                        await tractor.breakpoint()


def norm_trade_records(
    ledger: dict[str, Any],

) -> list[pp.Transaction]:

    records: list[pp.Transaction] = []

    for tid, record in ledger.items():

        size = record.get('vol') * {
            'buy': 1,
            'sell': -1,
        }[record['type']]
        bsuid = record['pair']
        norm_sym = normalize_symbol(bsuid)

        records.append(
            pp.Transaction(
                fqsn=f'{norm_sym}.kraken',
                tid=tid,
                size=float(size),
                price=float(record['price']),
                cost=float(record['fee']),
                dt=pendulum.from_timestamp(float(record['time'])),
                bsuid=bsuid,

                # XXX: there are no derivs on kraken right?
                # expiry=expiry,
            )
        )

    return records


async def update_ledger(
    acctid: str,
    trade_entries: list[dict[str, Any]],

) -> list[pp.Transaction]:

    # write recent session's trades to the user's (local) ledger file.
    with pp.open_trade_ledger(
        'kraken',
        acctid,
    ) as ledger:
        ledger.update(trade_entries)

    # normalize to transaction form
    records = norm_trade_records(trade_entries)
    return records