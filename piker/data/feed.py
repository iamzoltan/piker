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

"""
Data feed apis and infra.

This module is enabled for ``brokerd`` daemons.

"""
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from functools import partial
from types import ModuleType
from typing import (
    Any,
    AsyncIterator, Optional,
    Awaitable,
)

import trio
from trio.abc import ReceiveChannel
from trio_typing import TaskStatus
import tractor
from pydantic import BaseModel

from ..brokers import get_brokermod
from .._cacheables import maybe_open_context
from ..log import get_logger, get_console_log
from .._daemon import (
    maybe_spawn_brokerd,
)
from ._sharedmem import (
    maybe_open_shm_array,
    attach_shm_array,
    ShmArray,
)
from .ingest import get_ingestormod
from ._source import (
    base_iohlc_dtype,
    mk_symbol,
    Symbol,
    mk_fqsn,
)
from ..ui import _search
from ._sampling import (
    # TODO: should probably group these in a compound type at this point XD
    _ohlcv_shms,
    _subscribers,
    _incrementers,
    increment_ohlc_buffer,
    iter_ohlc_periods,
    sample_and_broadcast,
    uniform_rate_send,
)


log = get_logger(__name__)


class _FeedsBus(BaseModel):
    '''
    Data feeds broadcaster and persistence management.

    This is a brokerd side api used to manager persistent real-time
    streams that can be allocated and left alive indefinitely. A bus is
    associated one-to-one with a particular broker backend where the
    "bus" refers so a multi-symbol bus where quotes are interleaved in
    time.

    Each "entry" in the bus includes:
        - a stream used to push real time quotes (up to tick rates)
          which is executed as a lone task that is cancellable via
          a dedicated cancel scope.

    '''
    class Config:
        arbitrary_types_allowed = True
        underscore_attrs_are_private = False

    brokername: str
    nursery: trio.Nursery
    feeds: dict[str, tuple[dict, dict]] = {}

    task_lock: trio.StrictFIFOLock = trio.StrictFIFOLock()

    # XXX: so weird but, apparently without this being `._` private
    # pydantic will complain about private `tractor.Context` instance
    # vars (namely `._portal` and `._cancel_scope`) at import time.
    # Reported this bug:
    # https://github.com/samuelcolvin/pydantic/issues/2816
    _subscribers: dict[
        str,
        list[tuple[tractor.MsgStream, Optional[float]]]
    ] = {}

    async def start_task(
        self,
        target: Awaitable,
        *args,

    ) -> None:

        async def start_with_cs(
            task_status: TaskStatus[
                trio.CancelScope] = trio.TASK_STATUS_IGNORED,
        ) -> None:
            with trio.CancelScope() as cs:
                self.nursery.start_soon(
                    target,
                    *args,
                )
                task_status.started(cs)

        return await self.nursery.start(start_with_cs)

    def cancel_task(self, task: trio.Task) -> bool:
        pass


_bus: _FeedsBus = None


def get_feed_bus(
    brokername: str,
    nursery: Optional[trio.Nursery] = None,

) -> _FeedsBus:
    '''
    Retreive broker-daemon-local data feeds bus from process global
    scope. Serialize task access to lock.

    '''
    global _bus

    if nursery is not None:
        assert _bus is None, "Feeds manager is already setup?"

        # this is initial setup by parent actor
        _bus = _FeedsBus(
            brokername=brokername,
            nursery=nursery,
        )
        assert not _bus.feeds

    assert _bus.brokername == brokername, "Uhhh wtf"
    return _bus


@tractor.context
async def _setup_persistent_brokerd(
    ctx: tractor.Context,
    brokername: str,

) -> None:
    '''
    Allocate a actor-wide service nursery in ``brokerd``
    such that feeds can be run in the background persistently by
    the broker backend as needed.

    '''
    get_console_log(tractor.current_actor().loglevel)

    global _bus
    assert not _bus

    async with trio.open_nursery() as service_nursery:
        # assign a nursery to the feeds bus for spawning
        # background tasks from clients
        get_feed_bus(brokername, service_nursery)

        # unblock caller
        await ctx.started()

        # we pin this task to keep the feeds manager active until the
        # parent actor decides to tear it down
        await trio.sleep_forever()


async def manage_history(
    mod: ModuleType,
    shm: ShmArray,
    bus: _FeedsBus,
    symbol: str,
    we_opened_shm: bool,
    some_data_ready: trio.Event,
    feed_is_live: trio.Event,

    task_status: TaskStatus = trio.TASK_STATUS_IGNORED,

) -> None:
    '''
    Load and manage historical data including the loading of any
    available series from `marketstore` as well as conducting real-time
    update of both that existing db and the allocated shared memory
    buffer.

    '''
    # TODO: history retreival, see if we can pull from an existing
    # ``marketstored`` daemon
    log.info('Scanning for existing `marketstored`')
    fqsn = mk_fqsn(mod.name, symbol)
    # from .marketstore import manage_history
    # arrays = await manage_history(symbol)

    arrays = {}
    task_status.started()

    opened = we_opened_shm
    # TODO: history validation
    # assert opened, f'Persistent shm for {symbol} was already open?!'
    # if not opened:
    #     raise RuntimeError("Persistent shm for sym was already open?!")

    if opened:
        if arrays:
            await tractor.breakpoint()

            # push to shm
            # set data ready
            # some_data_ready.set()
            raise ValueError('this should never execute yet')

        else:
            # ask broker backend for new history

            # start history backfill task ``backfill_bars()`` is
            # a required backend func this must block until shm is
            # filled with first set of ohlc bars
            cs = await bus.nursery.start(mod.backfill_bars, symbol, shm)

    # indicate to caller that feed can be delivered to
    # remote requesting client since we've loaded history
    # data that can be used.
    some_data_ready.set()

    # detect sample step size for sampled historical data
    times = shm.array['time']
    delay_s = times[-1] - times[times != times[-1]][-1]

    # begin real-time updates of shm and tsb once the feed
    # goes live.
    await feed_is_live.wait()

    if opened:
        _ohlcv_shms.setdefault(delay_s, []).append(shm)

        # start shm incrementing for OHLC sampling at the current
        # detected sampling period if one dne.
        if _incrementers.get(delay_s) is None:
            cs = await bus.start_task(increment_ohlc_buffer, delay_s)

    await trio.sleep_forever()
    cs.cancel()


async def allocate_persistent_feed(

    bus: _FeedsBus,
    brokername: str,
    symbol: str,
    loglevel: str,
    task_status: TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED,

) -> None:
    '''
    Create and maintain a "feed bus" which allocates tasks for real-time
    streaming and optional historical data storage per broker/data provider
    backend; this normally task runs *in* a `brokerd` actor.

    If none exists, this allocates a ``_FeedsBus`` which manages the
    lifetimes of streaming tasks created for each requested symbol.


    2 tasks are created:
    - a real-time streaming task which connec

    '''
    try:
        mod = get_brokermod(brokername)
    except ImportError:
        mod = get_ingestormod(brokername)

    fqsn = mk_fqsn(brokername, symbol)

    # (maybe) allocate shm array for this broker/symbol which will
    # be used for fast near-term history capture and processing.
    shm, opened = maybe_open_shm_array(
        key=fqsn,

        # use any broker defined ohlc dtype:
        dtype=getattr(mod, '_ohlc_dtype', base_iohlc_dtype),

        # we expect the sub-actor to write
        readonly=False,
    )

    # mem chan handed to broker backend so it can push real-time
    # quotes to this task for sampling and history storage (see below).
    send, quote_stream = trio.open_memory_channel(10)

    # data sync signals for both history loading and market quotes
    some_data_ready = trio.Event()
    feed_is_live = trio.Event()

    # run 2 tasks:
    # - a history loader / maintainer
    # - a real-time streamer which consumers and sends new data to any
    #   consumers as well as writes to storage backends (as configured).

    # XXX: neither of these will raise but will cause an inf hang due to:
    # https://github.com/python-trio/trio/issues/2258
    # bus.nursery.start_soon(
    # await bus.start_task(

    await bus.nursery.start(
        manage_history,
        mod,
        shm,
        bus,
        symbol,
        opened,
        some_data_ready,
        feed_is_live,
    )

    # establish broker backend quote stream by calling
    # ``stream_quotes()``, which is a required broker backend endpoint.
    init_msg, first_quotes = await bus.nursery.start(
        partial(
            mod.stream_quotes,
            send_chan=send,
            feed_is_live=feed_is_live,
            symbols=[symbol],
            loglevel=loglevel,
        )
    )

    # we hand an IPC-msg compatible shm token to the caller so it
    # can read directly from the memory which will be written by
    # this task.
    init_msg[symbol]['shm_token'] = shm.token

    # TODO: pretty sure we don't need this? why not just leave 1s as
    # the fastest "sample period" since we'll probably always want that
    # for most purposes.
    # pass OHLC sample rate in seconds (be sure to use python int type)
    # init_msg[symbol]['sample_rate'] = 1 #int(delay_s)

    # yield back control to starting nursery once we receive either
    # some history or a real-time quote.
    log.info(f'waiting on history to load: {fqsn}')
    await some_data_ready.wait()

    bus.feeds[symbol.lower()] = (init_msg, first_quotes)
    task_status.started((init_msg,  first_quotes))

    # backend will indicate when real-time quotes have begun.
    await feed_is_live.wait()

    sum_tick_vlm: bool = init_msg.get(
        'shm_write_opts', {}
    ).get('sum_tick_vlm', True)

    # start sample loop
    try:
        await sample_and_broadcast(
            bus,
            shm,
            quote_stream,
            sum_tick_vlm
        )
    finally:
        log.warning(f'{symbol}@{brokername} feed task terminated')


@tractor.context
async def open_feed_bus(

    ctx: tractor.Context,
    brokername: str,
    symbol: str,
    loglevel: str,
    tick_throttle:  Optional[float] = None,
    start_stream: bool = True,

) -> None:
    '''
    Open a data feed "bus": an actor-persistent per-broker task-oriented
    data feed registry which allows managing real-time quote streams per
    symbol.

    '''
    if loglevel is None:
        loglevel = tractor.current_actor().loglevel

    # XXX: required to propagate ``tractor`` loglevel to piker logging
    get_console_log(loglevel or tractor.current_actor().loglevel)

    # local state sanity checks
    # TODO: check for any stale shm entries for this symbol
    # (after we also group them in a nice `/dev/shm/piker/` subdir).
    # ensure we are who we think we are
    assert 'brokerd' in tractor.current_actor().name

    bus = get_feed_bus(brokername)
    bus._subscribers.setdefault(symbol, [])
    fqsn = mk_fqsn(brokername, symbol)

    entry = bus.feeds.get(symbol)

    # if no cached feed for this symbol has been created for this
    # brokerd yet, start persistent stream and shm writer task in
    # service nursery
    if entry is None:
        if not start_stream:
            raise RuntimeError(
                f'No stream feed exists for {fqsn}?\n'
                f'You may need a `brokerd` started first.'
            )

        # allocate a new actor-local stream bus which will persist for
        # this `brokerd`.
        async with bus.task_lock:
            init_msg, first_quotes = await bus.nursery.start(
                partial(
                    allocate_persistent_feed,

                    bus=bus,
                    brokername=brokername,

                    # here we pass through the selected symbol in native
                    # "format" (i.e. upper vs. lowercase depending on
                    # provider).
                    symbol=symbol,

                    loglevel=loglevel,
                )
            )
            # TODO: we can remove this?
            assert isinstance(bus.feeds[symbol], tuple)

    # XXX: ``first_quotes`` may be outdated here if this is secondary
    # subscriber
    init_msg, first_quotes = bus.feeds[symbol]

    # send this even to subscribers to existing feed?
    # deliver initial info message a first quote asap
    await ctx.started((init_msg, first_quotes))

    if not start_stream:
        log.warning(f'Not opening real-time stream for {fqsn}')
        await trio.sleep_forever()

    # real-time stream loop
    async with (
        ctx.open_stream() as stream,
    ):
        if tick_throttle:

            # open a bg task which receives quotes over a mem chan
            # and only pushes them to the target actor-consumer at
            # a max ``tick_throttle`` instantaneous rate.

            send, recv = trio.open_memory_channel(2**10)
            cs = await bus.start_task(
                uniform_rate_send,
                tick_throttle,
                recv,
                stream,
            )
            sub = (send, tick_throttle)

        else:
            sub = (stream, tick_throttle)

        subs = bus._subscribers[symbol]
        subs.append(sub)

        try:
            uid = ctx.chan.uid

            async for msg in stream:

                if msg == 'pause':
                    if sub in subs:
                        log.info(
                            f'Pausing {fqsn} feed for {uid}')
                        subs.remove(sub)

                elif msg == 'resume':
                    if sub not in subs:
                        log.info(
                            f'Resuming {fqsn} feed for {uid}')
                        subs.append(sub)
                else:
                    raise ValueError(msg)
        finally:
            log.info(
                f'Stopping {symbol}.{brokername} feed for {ctx.chan.uid}')

            if tick_throttle:
                # TODO: a one-cancels-one nursery
                # n.cancel_scope.cancel()
                cs.cancel()
            try:
                bus._subscribers[symbol].remove(sub)
            except ValueError:
                log.warning(f'{sub} for {symbol} was already removed?')


@asynccontextmanager
async def open_sample_step_stream(
    portal: tractor.Portal,
    delay_s: int,

) -> tractor.ReceiveMsgStream:

    # XXX: this should be singleton on a host,
    # a lone broker-daemon per provider should be
    # created for all practical purposes
    async with maybe_open_context(
        acm_func=partial(
            portal.open_stream_from,
            iter_ohlc_periods,
        ),
        kwargs={'delay_s': delay_s},

    ) as (cache_hit, istream):
        if cache_hit:
            # add a new broadcast subscription for the quote stream
            # if this feed is likely already in use
            async with istream.subscribe() as bistream:
                yield bistream
        else:
            yield istream


@dataclass
class Feed:
    '''
    A data feed for client-side interaction with far-process real-time
    data sources.

    This is an thin abstraction on top of ``tractor``'s portals for
    interacting with IPC streams and storage APIs (shm and time-series
    db).

    '''
    name: str
    shm: ShmArray
    mod: ModuleType
    first_quotes: dict  # symbol names to first quote dicts

    _portal: tractor.Portal

    stream: trio.abc.ReceiveChannel[dict[str, Any]]
    throttle_rate: Optional[int] = None

    _trade_stream: Optional[AsyncIterator[dict[str, Any]]] = None
    _max_sample_rate: int = 1

    # cache of symbol info messages received as first message when
    # a stream startsc.
    symbols: dict[str, Symbol] = field(default_factory=dict)

    @property
    def portal(self) -> tractor.Portal:
        return self._portal

    async def receive(self) -> dict:
        return await self.stream.receive()

    @asynccontextmanager
    async def index_stream(
        self,
        delay_s: Optional[int] = None

    ) -> AsyncIterator[int]:

        delay_s = delay_s or self._max_sample_rate

        async with open_sample_step_stream(
            self.portal,
            delay_s,
        ) as istream:
            yield istream

    async def pause(self) -> None:
        await self.stream.send('pause')

    async def resume(self) -> None:
        await self.stream.send('resume')


@asynccontextmanager
async def install_brokerd_search(

    portal: tractor.Portal,
    brokermod: ModuleType,

) -> None:

    async with portal.open_context(
        brokermod.open_symbol_search
    ) as (ctx, cache):

        # shield here since we expect the search rpc to be
        # cancellable by the user as they see fit.
        async with ctx.open_stream() as stream:

            async def search(text: str) -> dict[str, Any]:
                await stream.send(text)
                return await stream.receive()

            async with _search.register_symbol_search(

                provider_name=brokermod.name,
                search_routine=search,

                # TODO: should be make this a required attr of
                # a backend module?
                pause_period=getattr(
                    brokermod, '_search_conf', {}
                    ).get('pause_period', 0.0616),
            ):
                yield


@asynccontextmanager
async def open_feed(
    brokername: str,
    symbols: list[str],
    loglevel: Optional[str] = None,

    backpressure: bool = True,
    start_stream: bool = True,
    tick_throttle: Optional[float] = None,  # Hz

) -> Feed:
    '''
    Open a "data feed" which provides streamed real-time quotes.

    '''
    sym = symbols[0].lower()

    try:
        mod = get_brokermod(brokername)
    except ImportError:
        mod = get_ingestormod(brokername)

    # no feed for broker exists so maybe spawn a data brokerd
    async with (

        # if no `brokerd` for this backend exists yet we spawn
        # and actor for one.
        maybe_spawn_brokerd(
            brokername,
            loglevel=loglevel
        ) as portal,

        # (allocate and) connect to any feed bus for this broker
        portal.open_context(
            open_feed_bus,
            brokername=brokername,
            symbol=sym,
            loglevel=loglevel,
            start_stream=start_stream,
            tick_throttle=tick_throttle,

        ) as (ctx, (init_msg, first_quotes)),

        ctx.open_stream(
            # XXX: be explicit about stream backpressure since we should
            # **never** overrun on feeds being too fast, which will
            # pretty much always happen with HFT XD
            backpressure=backpressure,
        ) as stream,

    ):
        # we can only read from shm
        shm = attach_shm_array(
            token=init_msg[sym]['shm_token'],
            readonly=True,
        )

        feed = Feed(
            name=brokername,
            shm=shm,
            mod=mod,
            first_quotes=first_quotes,
            stream=stream,
            _portal=portal,
            throttle_rate=tick_throttle,
        )

        for sym, data in init_msg.items():

            si = data['symbol_info']

            symbol = mk_symbol(
                key=sym,
                type_key=si.get('asset_type', 'forex'),
                tick_size=si.get('price_tick_size', 0.01),
                lot_tick_size=si.get('lot_tick_size', 0.0),
            )
            symbol.broker_info[brokername] = si

            feed.symbols[sym] = symbol

            # cast shm dtype to list... can't member why we need this
            shm_token = data['shm_token']

            # XXX: msgspec won't relay through the tuples XD
            shm_token['dtype_descr'] = tuple(
                map(tuple, shm_token['dtype_descr']))

            assert shm_token == shm.token  # sanity

        feed._max_sample_rate = 1

        try:
            yield feed
        finally:
            # drop the infinite stream connection
            await ctx.cancel()


@asynccontextmanager
async def maybe_open_feed(

    brokername: str,
    symbols: list[str],
    loglevel: Optional[str] = None,

    **kwargs,

) -> (Feed, ReceiveChannel[dict[str, Any]]):
    '''
    Maybe open a data to a ``brokerd`` daemon only if there is no
    local one for the broker-symbol pair, if one is cached use it wrapped
    in a tractor broadcast receiver.

    '''
    sym = symbols[0].lower()

    async with maybe_open_context(
        acm_func=open_feed,
        kwargs={
            'brokername': brokername,
            'symbols': [sym],
            'loglevel': loglevel,
            'tick_throttle': kwargs.get('tick_throttle'),

            # XXX: super critical to have bool defaults here XD
            'backpressure': kwargs.get('backpressure', True),
            'start_stream': kwargs.get('start_stream', True),
        },
        key=sym,
    ) as (cache_hit, feed):

        if cache_hit:
            log.info(f'Using cached feed for {brokername}.{sym}')
            # add a new broadcast subscription for the quote stream
            # if this feed is likely already in use
            async with feed.stream.subscribe() as bstream:
                yield feed, bstream
        else:
            yield feed, feed.stream
