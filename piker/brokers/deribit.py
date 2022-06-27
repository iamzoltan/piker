# piker: trading gear for hackers
# Copyright (C) Guillermo Rodriguez (in stewardship for piker0)

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
Deribit backend

"""
import asyncio
from async_generator import aclosing
from contextlib import asynccontextmanager as acm
from datetime import datetime
from typing import (
    Any, Union, Optional, List,
    AsyncGenerator, Callable,
)
import time

import trio
from trio_typing import TaskStatus
import pendulum
import asks
from fuzzywuzzy import process as fuzzy
import numpy as np
import tractor
from tractor import to_asyncio
from pydantic.dataclasses import dataclass
from pydantic import BaseModel
import wsproto

from .. import config
from .._cacheables import open_cached_client
from ._util import resproc, SymbolNotFound
from ..log import get_logger, get_console_log
from ..data import ShmArray
from ..data._web_bs import open_autorecon_ws, NoBsWs


from cryptofeed import FeedHandler

from cryptofeed.callback import (
    L1BookCallback,
    TradeCallback
)
from cryptofeed.defines import (
    DERIBIT, L1_BOOK, TRADES, OPTION, CALL, PUT
)
from cryptofeed.symbols import Symbol

_spawn_kwargs = {
    'infect_asyncio': True,
}


def get_config() -> dict[str, Any]:

    conf, path = config.load()

    section = conf.get('deribit')

    if section is None:
        log.warning(f'No config section found for deribit in {path}')
        return {}

    conf['log'] = {}
    conf['log']['filename'] = '/tmp/feedhandler.log'
    conf['log']['level'] = 'WARNING'

    return conf 


log = get_logger(__name__)


_url = 'https://www.deribit.com'


# Broker specific ohlc schema (rest)
_ohlc_dtype = [
    ('index', int),
    ('time', int),
    ('open', float),
    ('high', float),
    ('low', float),
    ('close', float),
    ('volume', float),
    # ('bar_wap', float),  # will be zeroed by sampler if not filled
]


class JSONRPCResult(BaseModel):
    jsonrpc: str = '2.0'
    result: dict 
    usIn: int 
    usOut: int 
    usDiff: int 
    testnet: bool


class KLinesResult(BaseModel):
    close: List[float]
    cost: List[float]
    high: List[float]
    low: List[float]
    open: List[float]
    status: str
    ticks: List[int]
    volume: List[float]


class KLines(JSONRPCResult):
    result: KLinesResult


class Trade(BaseModel):
    trade_seq: int
    trade_id: str
    timestamp: int
    tick_direction: int
    price: float
    mark_price: float
    iv: float
    instrument_name: str
    index_price: float
    direction: str
    amount: float

class LastTradesResult(BaseModel):
    trades: List[Trade]
    has_more: bool

class LastTrades(JSONRPCResult):
    result: LastTradesResult


# convert datetime obj timestamp to unixtime in milliseconds
def deribit_timestamp(when):
    return int((when.timestamp() * 1000) + (when.microsecond / 1000))


def str_to_cb_sym(name: str) -> Symbol:
    base, strike_price, expiry_date, option_type = name.split('-')

    quote = base

    if option_type == 'put':
        option_type = PUT 
    elif option_type  == 'call':
        option_type = CALL
    else:
        raise BaseException("Couldn\'t parse option type")

    return Symbol(
        base, quote,
        type=OPTION,
        strike_price=strike_price,
        option_type=option_type,
        expiry_date=expiry_date,
        expiry_normalize=False)



def piker_sym_to_cb_sym(name: str) -> Symbol:
    base, expiry_date, strike_price, option_type = tuple(
        name.upper().split('-'))

    quote = base

    if option_type == 'P':
        option_type = PUT 
    elif option_type  == 'C':
        option_type = CALL
    else:
        raise BaseException("Couldn\'t parse option type")

    return Symbol(
        base, quote,
        type=OPTION,
        strike_price=strike_price,
        option_type=option_type,
        expiry_date=expiry_date.upper())


def cb_sym_to_deribit_inst(sym: Symbol):
    # cryptofeed normalized
    cb_norm = ['F', 'G', 'H', 'J', 'K', 'M', 'N', 'Q', 'U', 'V', 'X', 'Z']

    # deribit specific 
    months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
    
    exp = sym.expiry_date

    # YYMDD
    # 01234
    year, month, day = (
        exp[:2], months[cb_norm.index(exp[2:3])], exp[3:])

    otype = 'C' if sym.option_type == CALL else 'P'

    return f'{sym.base}-{day}{month}{year}-{sym.strike_price}-{otype}'


class Client:

    def __init__(self) -> None:
        self._sesh = asks.Session(connections=4)
        self._sesh.base_location = _url
        self._pairs: dict[str, Any] = {}

    async def _api(
        self,
        method: str,
        params: dict,
    ) -> dict[str, Any]:
        resp = await self._sesh.get(
            path=f'/api/v2/public/{method}',
            params=params,
            timeout=float('inf')
        )
        return resproc(resp, log)

    async def symbol_info(
        self,
        instrument: Optional[str] = None,
        currency: str = 'btc',  # BTC, ETH, SOL, USDC
        kind: str = 'option',
        expired: bool = False
    ) -> dict[str, Any]:
        '''Get symbol info for the exchange.

        '''
        # TODO: we can load from our self._pairs cache
        # on repeat calls...

        # will retrieve all symbols by default
        params = {
            'currency': currency.upper(),
            'kind': kind,
            'expired': str(expired).lower()
        }

        resp = await self._api(
            'get_instruments', params=params)

        results = resp['result']

        instruments = {
            item['instrument_name']: item for item in results}

        if instrument is not None:
            return instruments[instrument]
        else:
            return instruments

    async def cache_symbols(
        self,
    ) -> dict:
        if not self._pairs:
            self._pairs = await self.symbol_info()

        return self._pairs

    async def search_symbols(
        self,
        pattern: str,
        limit: int = None,
    ) -> dict[str, Any]:
        if self._pairs is not None:
            data = self._pairs
        else:
            data = await self.symbol_info()

        matches = fuzzy.extractBests(
            pattern,
            data,
            score_cutoff=50,
        )
        # repack in dict form
        return {item[0]['instrument_name']: item[0]
                for item in matches}

    async def bars(
        self,
        symbol: str,
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
        limit: int = 1000,
        as_np: bool = True,
    ) -> dict:
        instrument = symbol

        if end_dt is None:
            end_dt = pendulum.now('UTC')

        if start_dt is None:
            start_dt = end_dt.start_of(
                'minute').subtract(minutes=limit)

        start_time = deribit_timestamp(start_dt)
        end_time = deribit_timestamp(end_dt)

        # https://docs.deribit.com/#public-get_tradingview_chart_data
        response = await self._api(
            'get_tradingview_chart_data',
            params={
                'instrument_name': instrument.upper(),
                'start_timestamp': start_time,
                'end_timestamp': end_time,
                'resolution': '1'
            }
        )

        klines = KLines(**response)
    
        result = klines.result
        new_bars = []
        for i in range(len(result.close)):

            _open = result.open[i]
            high = result.high[i]
            low = result.low[i]
            close = result.close[i]
            volume = result.volume[i]

            row = [
                (start_time + (i * (60 * 1000))) / 1000.0,  # time
                result.open[i],
                result.high[i],
                result.low[i],
                result.close[i],
                result.volume[i]
            ]

            new_bars.append((i,) + tuple(row))

        array = np.array(new_bars, dtype=_ohlc_dtype) if as_np else klines
        return array

    async def last_trades(
        self,
        instrument: str,
        count: int = 10
    ):
        response = await self._api(
            'get_last_trades_by_instrument',
            params={
                'instrument_name': instrument,
                'count': count
            }
        )

        return LastTrades(**response)


@acm
async def get_client() -> Client:
    client = Client()
    await client.cache_symbols()
    yield client


# inside here we are in an asyncio context
async def open_aio_cryptofeed_relay(
    from_trio: asyncio.Queue,
    to_trio: trio.abc.SendChannel,
    instruments: List[str] = []
) -> None:

    instruments = [piker_sym_to_cb_sym(i) for i in instruments]

    async def trade_cb(data: dict, receipt_timestamp):
        to_trio.send_nowait(('trade', {
            'symbol': cb_sym_to_deribit_inst(
                str_to_cb_sym(data.symbol)).lower(),
            'last': data,
            'broker_ts': time.time(),
            'data': data.to_dict(),
            'receipt': receipt_timestamp
        }))

    async def l1_book_cb(data: dict, receipt_timestamp):
        to_trio.send_nowait(('l1', {
            'symbol': cb_sym_to_deribit_inst(
                str_to_cb_sym(data.symbol)).lower(),
            'ticks': [
                {'type': 'bid',
                    'price': float(data.bid_price), 'size': float(data.bid_size)},
                {'type': 'bsize',
                    'price': float(data.bid_price), 'size': float(data.bid_size)},
                {'type': 'ask',
                    'price': float(data.ask_price), 'size': float(data.ask_size)},
                {'type': 'asize',
                    'price': float(data.ask_price), 'size': float(data.ask_size)}
            ]
        }))

    fh = FeedHandler(config=get_config())
    fh.run(start_loop=False)

    fh.add_feed(
        DERIBIT,
        channels=[L1_BOOK],
        symbols=instruments,
        callbacks={L1_BOOK: l1_book_cb})

    fh.add_feed(
        DERIBIT,
        channels=[TRADES],
        symbols=instruments,
        callbacks={TRADES: trade_cb})

    # sync with trio
    to_trio.send_nowait(None)

    await asyncio.sleep(float('inf'))


@acm
async def open_cryptofeeds(

    instruments: List[str]

) -> trio.abc.ReceiveStream:

    async with to_asyncio.open_channel_from(
        open_aio_cryptofeed_relay,
        instruments=instruments,
    ) as (first, chan):
        yield chan


@acm
async def open_history_client(
    instrument: str,
) -> tuple[Callable, int]:

    # TODO implement history getter for the new storage layer.
    async with open_cached_client('deribit') as client:

        async def get_ohlc(
            end_dt: Optional[datetime] = None,
            start_dt: Optional[datetime] = None,

        ) -> tuple[
            np.ndarray,
            datetime,  # start
            datetime,  # end
        ]:

            array = await client.bars(
                instrument,
                start_dt=start_dt,
                end_dt=end_dt,
            )
            start_dt = pendulum.from_timestamp(array[0]['time'])
            end_dt = pendulum.from_timestamp(array[-1]['time'])
            return array, start_dt, end_dt

        yield get_ohlc, {'erlangs': 3, 'rate': 3}


async def backfill_bars(
    symbol: str,
    shm: ShmArray,  # type: ignore # noqa
    task_status: TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED,
) -> None:
    """Fill historical bars into shared mem / storage afap.
    """
    instrument = symbol
    with trio.CancelScope() as cs:
        async with open_cached_client('deribit') as client:
            bars = await client.bars(instrument)
            shm.push(bars)
            task_status.started(cs)


async def stream_quotes(

    send_chan: trio.abc.SendChannel,
    symbols: list[str],
    feed_is_live: trio.Event,
    loglevel: str = None,

    # startup sync
    task_status: TaskStatus[tuple[dict, dict]] = trio.TASK_STATUS_IGNORED,

) -> None:
    # XXX: required to propagate ``tractor`` loglevel to piker logging
    get_console_log(loglevel or tractor.current_actor().loglevel)

    sym = symbols[0]

    async with (
        open_cached_client('deribit') as client,
        send_chan as send_chan,
        trio.open_nursery() as n,
        open_cryptofeeds(symbols) as stream 
    ):

        init_msgs = {
            # pass back token, and bool, signalling if we're the writer
            # and that history has been written
            sym: {
                'symbol_info': {
                    'asset_type': 'option'
                },
                'shm_write_opts': {'sum_tick_vml': False},
                'fqsn': sym,
            },
        }

        nsym = piker_sym_to_cb_sym(sym)

        # keep client cached for real-time section
        cache = await client.cache_symbols()

        last_trade = (await client.last_trades(
            cb_sym_to_deribit_inst(nsym), count=1)).result.trades[0]

        first_quote = {
            'symbol': sym,
            'last': last_trade.price,
            'brokerd_ts': last_trade.timestamp,
            'ticks': [{
                'type': 'trade',
                'price': last_trade.price,
                'size': last_trade.amount,
                'broker_ts': last_trade.timestamp
            }]
        }
        task_status.started((init_msgs,  first_quote))

        async with aclosing(stream):
            feed_is_live.set()

            async for typ, quote in stream:
                topic = quote['symbol']
                await send_chan.send({topic: quote})


@tractor.context
async def open_symbol_search(
    ctx: tractor.Context,
) -> Client:
    async with open_cached_client('deribit') as client:

        # load all symbols locally for fast search
        cache = await client.cache_symbols()
        await ctx.started()

        async with ctx.open_stream() as stream:

            async for pattern in stream:
                # results = await client.symbol_info(sym=pattern.upper())

                matches = fuzzy.extractBests(
                    pattern,
                    cache,
                    score_cutoff=50,
                )
                # repack in dict form
                await stream.send(
                    {item[0]['instrument_name']: item[0]
                     for item in matches}
                )
