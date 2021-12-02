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
Interactive Brokers API backend.

Note the client runs under an ``asyncio`` loop (since ``ib_insync`` is
built on it) and thus actor aware API calls must be spawned with
``infected_aio==True``.
"""
from contextlib import asynccontextmanager as acm
from dataclasses import asdict
from datetime import datetime
from functools import partial
import itertools
from math import isnan
from typing import (
    Any, Optional,
    AsyncIterator, Awaitable,
)
import asyncio
from pprint import pformat
import inspect
import logging
import platform
from random import randint
import time

import trio
from trio_typing import TaskStatus
import tractor
from async_generator import aclosing
from ib_insync.wrapper import RequestError
from ib_insync.contract import Contract, ContractDetails, Option
from ib_insync.order import Order, Trade, OrderStatus
from ib_insync.objects import Fill, Execution
from ib_insync.ticker import Ticker
from ib_insync.objects import Position
import ib_insync as ibis
from ib_insync.wrapper import Wrapper
from ib_insync.client import Client as ib_Client
from fuzzywuzzy import process as fuzzy
import numpy as np

from .. import config
from ..log import get_logger, get_console_log
from .._daemon import maybe_spawn_brokerd
from ..data._source import from_df
from ..data._sharedmem import ShmArray
from ._util import SymbolNotFound, NoData
from ..clearing._messages import (
    BrokerdOrder, BrokerdOrderAck, BrokerdStatus,
    BrokerdPosition, BrokerdCancel,
    BrokerdFill, BrokerdError,
)


log = get_logger(__name__)

# passed to ``tractor.ActorNursery.start_actor()``
_spawn_kwargs = {
    'infect_asyncio': True,
}
_time_units = {
    's': ' sec',
    'm': ' mins',
    'h': ' hours',
}

_time_frames = {
    '1s': '1 Sec',
    '5s': '5 Sec',
    '30s': '30 Sec',
    '1m': 'OneMinute',
    '2m': 'TwoMinutes',
    '3m': 'ThreeMinutes',
    '4m': 'FourMinutes',
    '5m': 'FiveMinutes',
    '10m': 'TenMinutes',
    '15m': 'FifteenMinutes',
    '20m': 'TwentyMinutes',
    '30m': 'HalfHour',
    '1h': 'OneHour',
    '2h': 'TwoHours',
    '4h': 'FourHours',
    'D': 'OneDay',
    'W': 'OneWeek',
    'M': 'OneMonth',
    'Y': 'OneYear',
}

_show_wap_in_history: bool = False

# optional search config the backend can register for
# it's symbol search handling (in this case we avoid
# accepting patterns before the kb has settled more then
# a quarter second).
_search_conf = {
    'pause_period': 6/16,
}


# overrides to sidestep pretty questionable design decisions in
# ``ib_insync``:

class NonShittyWrapper(Wrapper):
    def tcpDataArrived(self):
        """Override time stamps to be floats for now.
        """
        # use a ns int to store epoch time instead of datetime
        self.lastTime = time.time_ns()

        for ticker in self.pendingTickers:
            ticker.rtTime = None
            ticker.ticks = []
            ticker.tickByTicks = []
            ticker.domTicks = []
        self.pendingTickers = set()

    def execDetails(
        self,
        reqId: int,
        contract: Contract,
        execu,
    ):
        """
        Get rid of datetime on executions.
        """
        # this is the IB server's execution time supposedly
        # https://interactivebrokers.github.io/tws-api/classIBApi_1_1Execution.html#a2e05cace0aa52d809654c7248e052ef2
        execu.time = execu.time.timestamp()
        return super().execDetails(reqId, contract, execu)


class NonShittyIB(ibis.IB):
    """The beginning of overriding quite a few decisions in this lib.

    - Don't use datetimes
    - Don't use named tuples
    """
    def __init__(self):
        self._createEvents()
        # XXX: just to override this wrapper
        self.wrapper = NonShittyWrapper(self)
        self.client = ib_Client(self.wrapper)
        # self.errorEvent += self._onError
        self.client.apiEnd += self.disconnectedEvent
        self._logger = logging.getLogger('ib_insync.ib')


# map of symbols to contract ids
_adhoc_cmdty_data_map = {
    # https://misc.interactivebrokers.com/cstools/contract_info/v3.10/index.php?action=Conid%20Info&wlId=IB&conid=69067924

    # NOTE: some cmdtys/metals don't have trade data like gold/usd:
    # https://groups.io/g/twsapi/message/44174
    'XAUUSD': ({'conId': 69067924}, {'whatToShow': 'MIDPOINT'}),
}

_adhoc_futes_set = {

    # equities
    'nq.globex',
    'mnq.globex',

    'es.globex',
    'mes.globex',

    # cypto$
    'brr.cmecrypto',
    'ethusdrr.cmecrypto',

    # agriculture
    'he.globex',  # lean hogs
    'le.globex',  # live cattle (geezers)
    'gf.globex',  # feeder cattle (younguns)

    # raw
    'lb.globex',  # random len lumber

    # metals
    'xauusd.cmdty',  # gold spot
    'gc.nymex',
    'mgc.nymex',

    'xagusd.cmdty',  # silver spot
    'ni.nymex',  # silver futes
    'qi.comex',  # mini-silver futes
}

# exchanges we don't support at the moment due to not knowing
# how to do symbol-contract lookup correctly likely due
# to not having the data feeds subscribed.
_exch_skip_list = {
    'ASX',  # aussie stocks
    'MEXI',  # mexican stocks
    'VALUE',  # no idea
}

# https://misc.interactivebrokers.com/cstools/contract_info/v3.10/index.php?action=Conid%20Info&wlId=IB&conid=69067924

_enters = 0


class Client:
    """IB wrapped for our broker backend API.

    Note: this client requires running inside an ``asyncio`` loop.

    """
    _contracts: dict[str, Contract] = {}

    def __init__(
        self,

        ib: ibis.IB,

    ) -> None:
        self.ib = ib
        self.ib.RaiseRequestErrors = True

        # contract cache
        self._feeds: dict[str, trio.abc.SendChannel] = {}

        # NOTE: the ib.client here is "throttled" to 45 rps by default

    async def bars(
        self,
        symbol: str,
        # EST in ISO 8601 format is required... below is EPOCH
        start_dt: str = "1970-01-01T00:00:00.000000-05:00",
        end_dt: str = "",

        sample_period_s: str = 1,  # ohlc sample period
        period_count: int = int(2e3),  # <- max per 1s sample query

        is_paid_feed: bool = False,  # placeholder
    ) -> list[dict[str, Any]]:
        """Retreive OHLCV bars for a symbol over a range to the present.
        """
        bars_kwargs = {'whatToShow': 'TRADES'}

        global _enters
        print(f'ENTER BARS {_enters}')
        _enters += 1

        contract = await self.find_contract(symbol)
        bars_kwargs.update(getattr(contract, 'bars_kwargs', {}))

        # _min = min(2000*100, count)
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end_dt,

            # time history length values format:
            # ``durationStr=integer{SPACE}unit (S|D|W|M|Y)``

            # OHLC sampling values:
            # 1 secs, 5 secs, 10 secs, 15 secs, 30 secs, 1 min, 2 mins,
            # 3 mins, 5 mins, 10 mins, 15 mins, 20 mins, 30 mins,
            # 1 hour, 2 hours, 3 hours, 4 hours, 8 hours, 1 day, 1W, 1M
            # barSizeSetting='1 secs',

            # durationStr='{count} S'.format(count=15000 * 5),
            # durationStr='{count} D'.format(count=1),
            # barSizeSetting='5 secs',

            durationStr='{count} S'.format(count=period_count),
            # barSizeSetting='5 secs',
            barSizeSetting='1 secs',

            # barSizeSetting='1 min',

            # always use extended hours
            useRTH=False,

            # restricted per contract type
            **bars_kwargs,
            # whatToShow='MIDPOINT',
            # whatToShow='TRADES',
        )
        if not bars:
            # TODO: raise underlying error here
            raise ValueError(f"No bars retreived for {symbol}?")

        # TODO: rewrite this faster with ``numba``
        # convert to pandas dataframe:
        df = ibis.util.df(bars)
        return bars, from_df(df)

    async def con_deats(
        self,
        contracts: list[Contract],

    ) -> dict[str, ContractDetails]:

        futs = []
        for con in contracts:
            if con.primaryExchange not in _exch_skip_list:
                futs.append(self.ib.reqContractDetailsAsync(con))

        # batch request all details
        results = await asyncio.gather(*futs)

        # XXX: if there is more then one entry in the details list
        details = {}
        for details_set in results:

            # then the contract is so called "ambiguous".
            for d in details_set:
                con = d.contract
                unique_sym = f'{con.symbol}.{con.primaryExchange}'

                as_dict = asdict(d)
                # nested dataclass we probably don't need and that
                # won't IPC serialize
                as_dict.pop('secIdList')

                details[unique_sym] = as_dict

        return details

    async def search_stocks(
        self,
        pattern: str,

        get_details: bool = False,
        # how many contracts to search "up to"
        upto: int = 3,

    ) -> dict[str, ContractDetails]:
        """Search for stocks matching provided ``str`` pattern.

        Return a dictionary of ``upto`` entries worth of contract details.
        """
        descriptions = await self.ib.reqMatchingSymbolsAsync(pattern)

        if descriptions is not None:
            descrs = descriptions[:upto]

            if get_details:
                return await self.con_deats([d.contract for d in descrs])

            else:
                results = {}
                for d in descrs:
                    con = d.contract
                    # sometimes there's a weird extra suffix returned
                    # from search?
                    exch = con.primaryExchange.rsplit('.')[0]
                    unique_sym = f'{con.symbol}.{exch}'
                    results[unique_sym] = {}

                return results
        else:
            return {}

    async def search_symbols(
        self,
        pattern: str,
        # how many contracts to search "up to"
        upto: int = 3,
        asdicts: bool = True,

    ) -> dict[str, ContractDetails]:

        # TODO add search though our adhoc-locally defined symbol set
        # for futes/cmdtys/
        return await self.search_stocks(pattern, upto, get_details=True)

    async def get_cont_fute(
        self,
        symbol: str,
        exchange: str,
    ) -> Contract:
        """Get an unqualifed contract for the current "continous" future.
        """
        contcon = ibis.ContFuture(symbol, exchange=exchange)

        # it's the "front" contract returned here
        frontcon = (await self.ib.qualifyContractsAsync(contcon))[0]
        return ibis.Future(conId=frontcon.conId)

    async def find_contract(
        self,
        symbol,
        currency: str = 'USD',
        **kwargs,
    ) -> Contract:

        # TODO: we can't use this currently because
        # ``wrapper.starTicker()`` currently cashes ticker instances
        # which means getting a singel quote will potentially look up
        # a quote for a ticker that it already streaming and thus run
        # into state clobbering (eg. list: Ticker.ticks). It probably
        # makes sense to try this once we get the pub-sub working on
        # individual symbols...

        # XXX UPDATE: we can probably do the tick/trades scraping
        # inside our eventkit handler instead to bypass this entirely?

        # try:
        #     # give the cache a go
        #     return self._contracts[symbol]
        # except KeyError:
        #     log.debug(f'Looking up contract for {symbol}')

        # use heuristics to figure out contract "type"
        try:
            sym, exch = symbol.upper().rsplit('.', maxsplit=1)
        except ValueError:
            # likely there's an embedded `.` for a forex pair
            breakpoint()

        # futes
        if exch in ('GLOBEX', 'NYMEX', 'CME', 'CMECRYPTO'):
            con = await self.get_cont_fute(symbol=sym, exchange=exch)

        elif exch in ('FOREX'):
            currency = ''
            symbol, currency = sym.split('/')
            con = ibis.Forex(
                symbol=symbol,
                currency=currency,
            )
            con.bars_kwargs = {'whatToShow': 'MIDPOINT'}

        # commodities
        elif exch == 'CMDTY':  # eg. XAUUSD.CMDTY
            con_kwargs, bars_kwargs = _adhoc_cmdty_data_map[sym]
            con = ibis.Commodity(**con_kwargs)
            con.bars_kwargs = bars_kwargs

        # stonks
        else:
            # TODO: metadata system for all these exchange rules..
            primaryExchange = ''

            if exch in ('PURE', 'TSE'):  # non-yankee
                currency = 'CAD'
                # stupid ib...
                primaryExchange = exch
                exch = 'SMART'

            else:
                exch = 'SMART'
                primaryExchange = exch

            con = ibis.Stock(
                symbol=sym,
                exchange=exch,
                primaryExchange=primaryExchange,
                currency=currency,
            )
        try:
            exch = 'SMART' if not exch else exch
            contract = (await self.ib.qualifyContractsAsync(con))[0]

        except IndexError:
            raise ValueError(f"No contract could be found {con}")

        self._contracts[symbol] = contract
        return contract

    async def get_head_time(
        self,
        contract: Contract,
    ) -> datetime:
        """Return the first datetime stamp for ``contract``.

        """
        return await self.ib.reqHeadTimeStampAsync(
            contract,
            whatToShow='TRADES',
            useRTH=False,
            formatDate=2,  # timezone aware UTC datetime
        )

    async def get_quote(
        self,
        symbol: str,
    ) -> Ticker:
        """Return a single quote for symbol.

        """
        contract = await self.find_contract(symbol)

        details_fute = self.ib.reqContractDetailsAsync(contract)
        ticker: Ticker = self.ib.reqMktData(
            contract,
            snapshot=True,
        )

        # ensure a last price gets filled in before we deliver quote
        for _ in range(25):
            if isnan(ticker.last):
                ticker = await ticker.updateEvent
                await asyncio.sleep(0.2)
        else:
            log.warning(
                f'Symbol {symbol} is not returning a quote '
                'it may be outside trading hours?')

        details = (await details_fute)[0]
        return contract, ticker, details

    # async to be consistent for the client proxy, and cuz why not.
    def submit_limit(
        self,
        # ignored since ib doesn't support defining your
        # own order id
        oid: str,
        symbol: str,
        price: float,
        action: str,
        size: int,
        account: str,  # if blank the "default" tws account is used

        # XXX: by default 0 tells ``ib_insync`` methods that there is no
        # existing order so ask the client to create a new one (which it
        # seems to do by allocating an int counter - collision prone..)
        reqid: int = None,
    ) -> int:
        """Place an order and return integer request id provided by client.

        """
        try:
            contract = self._contracts[symbol]
        except KeyError:
            # require that the symbol has been previously cached by
            # a data feed request - ensure we aren't making orders
            # against non-known prices.
            raise RuntimeError("Can not order {symbol}, no live feed?")

        trade = self.ib.placeOrder(
            contract,
            Order(
                orderId=reqid or 0,  # stupid api devs..
                action=action.upper(),  # BUY/SELL
                # lookup the literal account number by name here.
                account=account,
                orderType='LMT',
                lmtPrice=price,
                totalQuantity=size,
                outsideRth=True,

                optOutSmartRouting=True,
                routeMarketableToBbo=True,
                designatedLocation='SMART',
            ),
        )

        # ib doesn't support setting your own id outside
        # their own weird client int counting ids..
        return trade.order.orderId

    def submit_cancel(
        self,
        reqid: str,
    ) -> None:
        """Send cancel request for order id ``oid``.

        """
        self.ib.cancelOrder(
            Order(
                orderId=reqid,
                clientId=self.ib.client.clientId,
            )
        )

    def inline_errors(
        self,
        to_trio: trio.abc.SendChannel,
    ) -> None:

        # connect error msgs
        def push_err(
            reqId: int,
            errorCode: int,
            errorString: str,
            contract: Contract,
        ) -> None:

            log.error(errorString)

            try:
                to_trio.send_nowait((
                    'error',

                    # error "object"
                    {'reqid': reqId,
                     'reason': errorString,
                     'contract': contract}
                ))
            except trio.BrokenResourceError:
                # XXX: eventkit's ``Event.emit()`` for whatever redic
                # reason will catch and ignore regular exceptions
                # resulting in tracebacks spammed to console..
                # Manually do the dereg ourselves.
                log.exception('Disconnected from errorEvent updates')
                self.ib.errorEvent.disconnect(push_err)

        self.ib.errorEvent.connect(push_err)

    def positions(
        self,
        account: str = '',

    ) -> list[Position]:
        """
        Retrieve position info for ``account``.
        """
        return self.ib.positions(account=account)


async def recv_trade_updates(

    client: Client,
    to_trio: trio.abc.SendChannel,

) -> None:
    """Stream a ticker using the std L1 api.
    """
    client.inline_errors(to_trio)

    # sync with trio task
    to_trio.send_nowait(None)

    def push_tradesies(eventkit_obj, obj, fill=None):
        """Push events to trio task.

        """
        if fill is not None:
            # execution details event
            item = ('fill', (obj, fill))

        elif eventkit_obj.name() == 'positionEvent':
            item = ('position', obj)

        else:
            item = ('status', obj)

        log.info(f'eventkit event ->\n{pformat(item)}')

        try:
            to_trio.send_nowait(item)
        except trio.BrokenResourceError:
            log.exception(f'Disconnected from {eventkit_obj} updates')
            eventkit_obj.disconnect(push_tradesies)

    # hook up to the weird eventkit object - event stream api
    for ev_name in [
        'orderStatusEvent',  # all order updates
        'execDetailsEvent',  # all "fill" updates
        'positionEvent',  # avg price updates per symbol per account

        # 'commissionReportEvent',
        # XXX: ugh, it is a separate event from IB and it's
        # emitted as follows:
        # self.ib.commissionReportEvent.emit(trade, fill, report)

        # XXX: not sure yet if we need these
        # 'updatePortfolioEvent',

        # XXX: these all seem to be weird ib_insync intrernal
        # events that we probably don't care that much about
        # given the internal design is wonky af..
        # 'newOrderEvent',
        # 'orderModifyEvent',
        # 'cancelOrderEvent',
        # 'openOrderEvent',
    ]:
        eventkit_obj = getattr(client.ib, ev_name)
        handler = partial(push_tradesies, eventkit_obj)
        eventkit_obj.connect(handler)

    # let the engine run and stream
    await client.ib.disconnectedEvent


# default config ports
_tws_port: int = 7497
_gw_port: int = 4002
_try_ports = [
    _gw_port,
    _tws_port
]
# TODO: remove the randint stuff and use proper error checking in client
# factor below..
_client_ids = itertools.count(randint(1, 100))
_client_cache: dict[tuple[str, int], Client] = {}
_scan_ignore: set[tuple[str, int]] = set()


def get_config() -> dict[str, Any]:

    conf, path = config.load()

    section = conf.get('ib')

    if section is None:
        log.warning(f'No config section found for ib in {path}')
        return {}

    return section


_accounts2clients: dict[str, Client] = {}


@acm
async def load_aio_clients(

    host: str = '127.0.0.1',
    port: int = None,

    client_id: Optional[int] = None,

) -> Client:
    '''Return an ``ib_insync.IB`` instance wrapped in our client API.

    Client instances are cached for later use.

    TODO: consider doing this with a ctx mngr eventually?
    '''
    global _accounts2clients, _client_cache, _scan_ignore

    conf = get_config()
    ib = None
    client = None

    # attempt to get connection info from config; if no .toml entry
    # exists, we try to load from a default localhost connection.
    localhost = '127.0.0.1'
    host, hosts = conf.get('host'), conf.get('hosts')
    if not (hosts or host):
        host = localhost

    if not hosts:
        hosts = [host]
    elif host and hosts:
        raise ValueError(
            'Specify only one of `host` or `hosts` in `brokers.toml` config')

    ports = conf.get(
        'ports',

        # default order is to check for gw first
        {
            'gw': 4002,
            'tws': 7497,
            # 'order': ['gw', 'tws']
        }
    )
    order = ports.pop('order', None)
    if order:
        log.warning('`ports.order` section in `brokers.toml` is deprecated')

    accounts_def = config.load_accounts(['ib'])
    try_ports = list(ports.values())
    ports = try_ports if port is None else [port]
    # we_connected = []
    connect_timeout = 0.5 if platform.system() != 'Windows' else 1
    combos = list(itertools.product(hosts, ports))

    # allocate new and/or reload disconnected but cached clients
    # try:
    # TODO: support multiple clients allowing for execution on
    # multiple accounts (including a paper instance running on the
    # same machine) and switching between accounts in the EMs

    _err = None

    # (re)load any and all clients that can be found
    # from connection details in ``brokers.toml``.
    for host, port in combos:

        sockaddr = (host, port)
        client = _client_cache.get(sockaddr)
        accounts_found: dict[str, Client] = {}

        if (
            client and client.ib.isConnected() or
            sockaddr in _scan_ignore
        ):
            continue

        try:
            ib = NonShittyIB()

            # XXX: not sure if we ever really need to increment the
            # client id if teardown is sucessful.
            client_id = 616

            await ib.connectAsync(
                host,
                port,
                clientId=client_id,

                # this timeout is sensative on windows and will
                # fail without a good "timeout error" so be
                # careful.
                timeout=connect_timeout,
            )

            # create and cache client
            client = Client(ib)

            # Pre-collect all accounts available for this
            # connection and map account names to this client
            # instance.
            pps = ib.positions()
            if pps:
                for pp in pps:
                    accounts_found[
                        accounts_def.inverse[pp.account]
                    ] = client

            # if there are no positions or accounts
            # without positions we should still register
            # them for this client
            for value in ib.accountValues():
                acct = value.account
                if acct not in accounts_found:
                    accounts_found[
                        accounts_def.inverse[acct]
                    ] = client

            log.info(
                f'Loaded accounts for client @ {host}:{port}\n'
                f'{pformat(accounts_found)}'
            )

            # update all actor-global caches
            log.info(f"Caching client for {(host, port)}")
            _client_cache[(host, port)] = client

            # we_connected.append((host, port, client))

            # TODO: don't do it this way, get a gud to_asyncio
            # context / .start() system goin..
            def pop_and_discon():
                log.info(f'Disconnecting client {client}')
                client.ib.disconnect()
                _client_cache.pop((host, port), None)

            # NOTE: the above callback **CAN'T FAIL** or shm won't get
            # torn down correctly ...
            tractor._actor._lifetime_stack.callback(pop_and_discon)

            _accounts2clients.update(accounts_found)

        except (
            ConnectionRefusedError,

            # TODO: if trying to scan for remote api clients
            # pretty sure we need to catch this, though it
            # definitely needs a shorter timeout since it hangs
            # for like 5s..
            asyncio.exceptions.TimeoutError,
            OSError,
        ) as ce:
            _err = ce
            log.warning(f'Failed to connect on {port}')

            # cache logic to avoid rescanning if we already have all
            # clients loaded.
            _scan_ignore.add(sockaddr)
    else:
        if not _client_cache:
            raise ConnectionError(
                'No ib APIs could be found scanning @:\n'
                f'{pformat(combos)}\n'
                'Check your `brokers.toml` and/or network'
            ) from _err

    # retreive first loaded client
    clients = list(_client_cache.values())
    if clients:
        client = clients[0]

    yield client, _client_cache, _accounts2clients

    # TODO: this in a way that works xD
    # finally:
    #     pass
    #     # async with trio.CancelScope(shield=True):
    #     for host, port, client in we_connected:
    #         client.ib.disconnect()
    #         _client_cache.pop((host, port))
    #     raise


async def _aio_run_client_method(
    meth: str,
    to_trio=None,
    from_trio=None,
    client=None,
    **kwargs,
) -> None:

    async with load_aio_clients() as (
        _client,
        clients,
        accts2clients,
    ):
        client = client or _client
        async_meth = getattr(client, meth)

        # handle streaming methods
        args = tuple(inspect.getfullargspec(async_meth).args)
        if to_trio and 'to_trio' in args:
            kwargs['to_trio'] = to_trio

        log.runtime(f'Running {meth}({kwargs})')
        return await async_meth(**kwargs)


async def _trio_run_client_method(
    method: str,
    client: Optional[Client] = None,
    **kwargs,

) -> None:
    """Asyncio entry point to run tasks against the ``ib_insync`` api.

    """
    ca = tractor.current_actor()
    assert ca.is_infected_aio()

    # if the method is an *async gen* stream for it
    # meth = getattr(Client, method)

    # args = tuple(inspect.getfullargspec(meth).args)

    # if inspect.isasyncgenfunction(meth) or (
    #     # if the method is an *async func* but manually
    #     # streams back results, make sure to also stream it
    #     'to_trio' in args
    # ):
    #     kwargs['_treat_as_stream'] = True

    return await tractor.to_asyncio.run_task(
        _aio_run_client_method,
        meth=method,
        client=client,
        **kwargs
    )


class _MethodProxy:
    def __init__(
        self,
        portal: tractor.Portal
    ) -> None:
        self._portal = portal

    async def _run_method(
        self,
        *,
        meth: str = None,
        **kwargs
    ) -> Any:
        return await self._portal.run(
            _trio_run_client_method,
            method=meth,
            **kwargs
        )


def get_client_proxy(

    portal: tractor.Portal,
    target=Client,

) -> _MethodProxy:

    proxy = _MethodProxy(portal)

    # mock all remote methods
    for name, method in inspect.getmembers(
        target, predicate=inspect.isfunction
    ):
        if '_' == name[0]:
            continue
        setattr(proxy, name, partial(proxy._run_method, meth=name))

    return proxy


@acm
async def get_client(
    **kwargs,
) -> Client:
    """Init the ``ib_insync`` client in another actor and return
    a method proxy to it.
    """
    async with maybe_spawn_brokerd(
        brokername='ib',
        infect_asyncio=True,
        **kwargs
    ) as portal:
        proxy_client = get_client_proxy(portal)
        yield proxy_client


# https://interactivebrokers.github.io/tws-api/tick_types.html
tick_types = {
    77: 'trade',
    48: 'utrade',
    0: 'bsize',
    1: 'bid',
    2: 'ask',
    3: 'asize',
    4: 'last',
    5: 'size',
    8: 'volume',
}


def normalize(
    ticker: Ticker,
    calc_price: bool = False
) -> dict:
    # convert named tuples to dicts so we send usable keys
    new_ticks = []
    for tick in ticker.ticks:
        if tick and not isinstance(tick, dict):
            td = tick._asdict()
            td['type'] = tick_types.get(td['tickType'], 'n/a')

            new_ticks.append(td)

    ticker.ticks = new_ticks

    # some contracts don't have volume so we may want to calculate
    # a midpoint price based on data we can acquire (such as bid / ask)
    if calc_price:
        ticker.ticks.append(
            {'type': 'trade', 'price': ticker.marketPrice()}
        )

    # serialize for transport
    data = asdict(ticker)

    # add time stamps for downstream latency measurements
    data['brokerd_ts'] = time.time()

    # stupid stupid shit...don't even care any more..
    # leave it until we do a proper latency study
    # if ticker.rtTime is not None:
    #     data['broker_ts'] = data['rtTime_s'] = float(
    #         ticker.rtTime.timestamp) / 1000.
    data.pop('rtTime')

    return data


async def get_bars(

    sym: str,
    end_dt: str = "",

) -> (dict, np.ndarray):

    _err: Optional[Exception] = None

    fails = 0
    for _ in range(2):
        try:

            bars, bars_array = await _trio_run_client_method(
                method='bars',
                symbol=sym,
                end_dt=end_dt,
            )

            if bars_array is None:
                raise SymbolNotFound(sym)

            next_dt = bars[0].date

            return (bars, bars_array, next_dt), fails

        except RequestError as err:
            _err = err

            # TODO: retreive underlying ``ib_insync`` error?
            if err.code == 162:

                # TODO: so this error is normally raised (it seems) if
                # we try to retrieve history for a time range for which
                # there is none. in that case we should not only report
                # the "empty range" but also do a iteration on the time
                # step for ``next_dt`` to see if we can pull older
                # history.
                if 'HMDS query returned no data' in err.message:
                    # means we hit some kind of historical "empty space"
                    # and further requests will need to decrement the
                    # start time dt in order to not receive a further
                    # error?
                    # OLDER: seem to always cause throttling despite low rps

                    # raise err
                    break

                elif 'No market data permissions for' in err.message:

                    # TODO: signalling for no permissions searches
                    raise NoData(f'Symbol: {sym}')
                    break

                else:
                    log.exception(
                        "Data query rate reached: Press `ctrl-alt-f`"
                        "in TWS"
                    )
                    print(_err)

                    # TODO: should probably create some alert on screen
                    # and then somehow get that to trigger an event here
                    # that restarts/resumes this task?
                    await tractor.breakpoint()
                    fails += 1
                    continue

    return None, None
    # else:  # throttle wasn't fixed so error out immediately
    #     raise _err


async def backfill_bars(

    sym: str,
    shm: ShmArray,  # type: ignore # noqa

    # TODO: we want to avoid overrunning the underlying shm array buffer
    # and we should probably calc the number of calls to make depending
    # on that until we have the `marketstore` daemon in place in which
    # case the shm size will be driven by user config and available sys
    # memory.
    count: int = 16,

    task_status: TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED,

) -> None:
    """Fill historical bars into shared mem / storage afap.

    TODO: avoid pacing constraints:
    https://github.com/pikers/piker/issues/128

    """
    out, fails = await get_bars(sym)

    if out is None:
        raise RuntimeError("Could not pull currrent history?!")

    (first_bars, bars_array, next_dt) = out
    vlm = bars_array['volume']
    vlm[vlm < 0] = 0

    # write historical data to buffer
    shm.push(bars_array)

    with trio.CancelScope() as cs:

        task_status.started(cs)

        i = 0
        while i < count:

            out, fails = await get_bars(sym, end_dt=next_dt)

            if fails is None or fails > 1:
                break

            if out == (None, None):
                # could be trying to retreive bars over weekend
                # TODO: add logic here to handle tradable hours and only grab
                # valid bars in the range
                log.error(f"Can't grab bars starting at {next_dt}!?!?")
                continue

            bars, bars_array, next_dt = out

            # volume cleaning since there's -ve entries,
            # wood luv to know what crookery that is..
            vlm = bars_array['volume']
            vlm[vlm < 0] = 0
            # TODO we should probably dig into forums to see what peeps
            # think this data "means" and then use it as an indicator of
            # sorts? dinkus has mentioned that $vlms for the day dont'
            # match other platforms nor the summary stat tws shows in
            # the monitor - it's probably worth investigating.

            shm.push(bars_array, prepend=True)
            i += 1


asset_type_map = {
    'STK': 'stock',
    'OPT': 'option',
    'FUT': 'future',
    'CONTFUT': 'continuous_future',
    'CASH': 'forex',
    'IND': 'index',
    'CFD': 'cfd',
    'BOND': 'bond',
    'CMDTY': 'commodity',
    'FOP': 'futures_option',
    'FUND': 'mutual_fund',
    'WAR': 'warrant',
    'IOPT': 'warran',
    'BAG': 'bag',
    # 'NEWS': 'news',
}


_quote_streams: dict[str, trio.abc.ReceiveStream] = {}


async def _setup_quote_stream(

    from_trio: asyncio.Queue,
    to_trio: trio.abc.SendChannel,

    symbol: str,
    opts: tuple[int] = ('375', '233', '236'),
    contract: Optional[Contract] = None,

) -> trio.abc.ReceiveChannel:
    """Stream a ticker using the std L1 api.
    """
    global _quote_streams

    to_trio.send_nowait(None)

    async with load_aio_clients() as (
        client,
        clients,
        accts2clients,
    ):
        contract = contract or (await client.find_contract(symbol))
        ticker: Ticker = client.ib.reqMktData(contract, ','.join(opts))

        # # define a simple queue push routine that streams quote packets
        # # to trio over the ``to_trio`` memory channel.
        # to_trio, from_aio = trio.open_memory_channel(2**8)  # type: ignore
        def teardown():
            ticker.updateEvent.disconnect(push)
            log.error(f"Disconnected stream for `{symbol}`")
            client.ib.cancelMktData(contract)

            # decouple broadcast mem chan
            _quote_streams.pop(symbol, None)

        def push(t: Ticker) -> None:
            """
            Push quotes to trio task.

            """
            # log.debug(t)
            try:
                to_trio.send_nowait(t)

            except (
                trio.BrokenResourceError,

                # XXX: HACK, not sure why this gets left stale (probably
                # due to our terrible ``tractor.to_asyncio``
                # implementation for streams.. but if the mem chan
                # gets left here and starts blocking just kill the feed?
                # trio.WouldBlock,
            ):
                # XXX: eventkit's ``Event.emit()`` for whatever redic
                # reason will catch and ignore regular exceptions
                # resulting in tracebacks spammed to console..
                # Manually do the dereg ourselves.
                teardown()

            # except trio.WouldBlock:
            #     # for slow debugging purposes to avoid clobbering prompt
            #     # with log msgs
            #     pass

        ticker.updateEvent.connect(push)
        try:
            await asyncio.sleep(float('inf'))
        finally:
            teardown()

        # return from_aio


@acm
async def open_aio_quote_stream(

    symbol: str,
    contract: Optional[Contract] = None,

) -> trio.abc.ReceiveStream:

    from tractor.trionics import broadcast_receiver
    global _quote_streams

    from_aio = _quote_streams.get(symbol)
    if from_aio:

        # if we already have a cached feed deliver a rx side clone to consumer
        async with broadcast_receiver(from_aio) as from_aio:
            yield from_aio
            return

    async with tractor.to_asyncio.open_channel_from(
        _setup_quote_stream,
        symbol=symbol,
        contract=contract,
    ) as (first, from_aio):

        # cache feed for later consumers
        _quote_streams[symbol] = from_aio

        yield from_aio


async def stream_quotes(

    send_chan: trio.abc.SendChannel,
    symbols: list[str],
    shm: ShmArray,
    feed_is_live: trio.Event,
    loglevel: str = None,

    # startup sync
    task_status: TaskStatus[tuple[dict, dict]] = trio.TASK_STATUS_IGNORED,

) -> None:
    """Stream symbol quotes.

    This is a ``trio`` callable routine meant to be invoked
    once the brokerd is up.
    """

    # TODO: support multiple subscriptions
    sym = symbols[0]

    with trio.fail_after(5):
        contract, first_ticker, details = await _trio_run_client_method(
            method='get_quote',
            symbol=sym,
        )

    # stream = await start_aio_quote_stream(symbol=sym, contract=contract)
    async with open_aio_quote_stream(
        symbol=sym, contract=contract
    ) as stream:

        # pass back some symbol info like min_tick, trading_hours, etc.
        syminfo = asdict(details)
        syminfo.update(syminfo['contract'])

        # nested dataclass we probably don't need and that won't IPC serialize
        syminfo.pop('secIdList')

        # TODO: more consistent field translation
        atype = syminfo['asset_type'] = asset_type_map[syminfo['secType']]

        # for stocks it seems TWS reports too small a tick size
        # such that you can't submit orders with that granularity?
        min_tick = 0.01 if atype == 'stock' else 0

        syminfo['price_tick_size'] = max(syminfo['minTick'], min_tick)

        # for "traditional" assets, volume is normally discreet, not a float
        syminfo['lot_tick_size'] = 0.0

        # TODO: for loop through all symbols passed in
        init_msgs = {
            # pass back token, and bool, signalling if we're the writer
            # and that history has been written
            sym: {
                'symbol_info': syminfo,
            }
        }

        con = first_ticker.contract

        # should be real volume for this contract by default
        calc_price = False

        # check for special contract types
        if type(first_ticker.contract) not in (ibis.Commodity, ibis.Forex):

            suffix = con.primaryExchange
            if not suffix:
                suffix = con.exchange

        else:
            # commodities and forex don't have an exchange name and
            # no real volume so we have to calculate the price
            suffix = con.secType
            # no real volume on this tract
            calc_price = True

        quote = normalize(first_ticker, calc_price=calc_price)
        con = quote['contract']
        topic = '.'.join((con['symbol'], suffix)).lower()
        quote['symbol'] = topic

        # pass first quote asap
        first_quote = {topic: quote}

        # ugh, clear ticks since we've consumed them
        # (ahem, ib_insync is stateful trash)
        first_ticker.ticks = []

        log.debug(f"First ticker received {quote}")

        task_status.started((init_msgs,  first_quote))

        async with aclosing(stream):
            if type(first_ticker.contract) not in (ibis.Commodity, ibis.Forex):
                # suffix = 'exchange'
                # calc_price = False  # should be real volume for contract

                # wait for real volume on feed (trading might be closed)
                while True:

                    ticker = await stream.receive()

                    # for a real volume contract we rait for the first
                    # "real" trade to take place
                    if not calc_price and not ticker.rtTime:
                        # spin consuming tickers until we get a real
                        # market datum
                        log.debug(f"New unsent ticker: {ticker}")
                        continue
                    else:
                        log.debug("Received first real volume tick")
                        # ugh, clear ticks since we've consumed them
                        # (ahem, ib_insync is truly stateful trash)
                        ticker.ticks = []

                        # XXX: this works because we don't use
                        # ``aclosing()`` above?
                        break

            # tell caller quotes are now coming in live
            feed_is_live.set()

            # last = time.time()
            async for ticker in stream:
                # print(f'ticker rate: {1/(time.time() - last)}')

                # print(ticker.vwap)
                quote = normalize(
                    ticker,
                    calc_price=calc_price
                )

                quote['symbol'] = topic
                await send_chan.send({topic: quote})

                # ugh, clear ticks since we've consumed them
                ticker.ticks = []
                # last = time.time()


def pack_position(
    pos: Position

) -> dict[str, Any]:
    con = pos.contract

    if isinstance(con, Option):
        # TODO: option symbol parsing and sane display:
        symbol = con.localSymbol.replace(' ', '')

    else:
        symbol = con.symbol.lower()

    exch = (con.primaryExchange or con.exchange).lower()
    symkey = '.'.join((symbol, exch))

    if not exch:
        # attempt to lookup the symbol from our
        # hacked set..
        for sym in _adhoc_futes_set:
            if symbol in sym:
                symkey = sym
                break

        # TODO: options contracts into a sane format..

    return BrokerdPosition(
        broker='ib',
        account=pos.account,
        symbol=symkey,
        currency=con.currency,
        size=float(pos.position),
        avg_price=float(pos.avgCost) / float(con.multiplier or 1.0),
    )


async def handle_order_requests(

    ems_order_stream: tractor.MsgStream,
    accounts_def: dict[str, str],

) -> None:

    global _accounts2clients

    # request_msg: dict
    async for request_msg in ems_order_stream:
        log.info(f'Received order request {request_msg}')

        action = request_msg['action']
        account = request_msg['account']

        acct_number = accounts_def.get(account)
        if not acct_number:
            log.error(
                f'An IB account number for name {account} is not found?\n'
                'Make sure you have all TWS and GW instances running.'
            )
            await ems_order_stream.send(BrokerdError(
                oid=request_msg['oid'],
                symbol=request_msg['symbol'],
                reason=f'No account found: `{account}` ?',
            ).dict())
            continue

        client = _accounts2clients.get(account)
        if not client:
            log.error(
                f'An IB client for account name {account} is not found.\n'
                'Make sure you have all TWS and GW instances running.'
            )
            await ems_order_stream.send(BrokerdError(
                oid=request_msg['oid'],
                symbol=request_msg['symbol'],
                reason=f'No api client loaded for account: `{account}` ?',
            ).dict())
            continue

        if action in {'buy', 'sell'}:
            # validate
            order = BrokerdOrder(**request_msg)

            # call our client api to submit the order
            reqid = client.submit_limit(
                oid=order.oid,
                symbol=order.symbol,
                price=order.price,
                action=order.action,
                size=order.size,
                account=acct_number,

                # XXX: by default 0 tells ``ib_insync`` methods that
                # there is no existing order so ask the client to create
                # a new one (which it seems to do by allocating an int
                # counter - collision prone..)
                reqid=order.reqid,
            )

            # deliver ack that order has been submitted to broker routing
            await ems_order_stream.send(
                BrokerdOrderAck(
                    # ems order request id
                    oid=order.oid,
                    # broker specific request id
                    reqid=reqid,
                    time_ns=time.time_ns(),
                    account=account,
                ).dict()
            )

        elif action == 'cancel':
            msg = BrokerdCancel(**request_msg)
            client.submit_cancel(reqid=msg.reqid)

        else:
            log.error(f'Unknown order command: {request_msg}')


@tractor.context
async def trades_dialogue(

    ctx: tractor.Context,
    loglevel: str = None,

) -> AsyncIterator[dict[str, Any]]:

    # XXX: required to propagate ``tractor`` loglevel to piker logging
    get_console_log(loglevel or tractor.current_actor().loglevel)

    accounts_def = config.load_accounts(['ib'])

    global _accounts2clients
    global _client_cache

    # deliver positions to subscriber before anything else
    all_positions = []
    accounts = set()

    clients: list[tuple[Client, trio.MemoryReceiveChannel]] = []
    async with trio.open_nursery() as nurse:
        for account, client in _accounts2clients.items():

            async def open_stream(
                task_status: TaskStatus[
                    trio.abc.ReceiveChannel
                ] = trio.TASK_STATUS_IGNORED,
            ):
                # each api client has a unique event stream
                async with tractor.to_asyncio.open_channel_from(
                    recv_trade_updates,
                    client=client,
                ) as (first, trade_event_stream):
                    task_status.started(trade_event_stream)
                    await trio.sleep_forever()

            trade_event_stream = await nurse.start(open_stream)

            clients.append((client, trade_event_stream))

        for client in _client_cache.values():
            for pos in client.positions():
                msg = pack_position(pos)
                msg.account = accounts_def.inverse[msg.account]
                accounts.add(msg.account)
                all_positions.append(msg.dict())

        await ctx.started((
            all_positions,
            tuple(name for name in accounts_def if name in accounts),
        ))

        async with (
            ctx.open_stream() as ems_stream,
            trio.open_nursery() as n,
        ):
            # start order request handler **before** local trades event loop
            n.start_soon(handle_order_requests, ems_stream, accounts_def)

            # allocate event relay tasks for each client connection
            for client, stream in clients:
                n.start_soon(
                    deliver_trade_events,
                    stream,
                    ems_stream,
                    accounts_def
                )

            # block until cancelled
            await trio.sleep_forever()


async def deliver_trade_events(

    trade_event_stream: trio.MemoryReceiveChannel,
    ems_stream: tractor.MsgStream,
    accounts_def: dict[str, str],

) -> None:
    '''Format and relay all trade events for a given client to the EMS.

    '''
    action_map = {'BOT': 'buy', 'SLD': 'sell'}

    # TODO: for some reason we can receive a ``None`` here when the
    # ib-gw goes down? Not sure exactly how that's happening looking
    # at the eventkit code above but we should probably handle it...
    async for event_name, item in trade_event_stream:

        log.info(f'ib sending {event_name}:\n{pformat(item)}')

        # TODO: templating the ib statuses in comparison with other
        # brokers is likely the way to go:
        # https://interactivebrokers.github.io/tws-api/interfaceIBApi_1_1EWrapper.html#a17f2a02d6449710b6394d0266a353313
        # short list:
        # - PendingSubmit
        # - PendingCancel
        # - PreSubmitted (simulated orders)
        # - ApiCancelled (cancelled by client before submission
        #                 to routing)
        # - Cancelled
        # - Filled
        # - Inactive (reject or cancelled but not by trader)

        # XXX: here's some other sucky cases from the api
        # - short-sale but securities haven't been located, in this
        #   case we should probably keep the order in some kind of
        #   weird state or cancel it outright?

        # status='PendingSubmit', message=''),
        # status='Cancelled', message='Error 404,
        #   reqId 1550: Order held while securities are located.'),
        # status='PreSubmitted', message='')],

        if event_name == 'status':

            # XXX: begin normalization of nonsense ib_insync internal
            # object-state tracking representations...

            # unwrap needed data from ib_insync internal types
            trade: Trade = item
            status: OrderStatus = trade.orderStatus

            # skip duplicate filled updates - we get the deats
            # from the execution details event
            msg = BrokerdStatus(

                reqid=trade.order.orderId,
                time_ns=time.time_ns(),  # cuz why not
                account=accounts_def.inverse[trade.order.account],

                # everyone doin camel case..
                status=status.status.lower(),  # force lower case

                filled=status.filled,
                reason=status.whyHeld,

                # this seems to not be necessarily up to date in the
                # execDetails event.. so we have to send it here I guess?
                remaining=status.remaining,

                broker_details={'name': 'ib'},
            )

        elif event_name == 'fill':

            # for wtv reason this is a separate event type
            # from IB, not sure why it's needed other then for extra
            # complexity and over-engineering :eyeroll:.
            # we may just end up dropping these events (or
            # translating them to ``Status`` msgs) if we can
            # show the equivalent status events are no more latent.

            # unpack ib_insync types
            # pep-0526 style:
            # https://www.python.org/dev/peps/pep-0526/#global-and-local-variable-annotations
            trade: Trade
            fill: Fill
            trade, fill = item
            execu: Execution = fill.execution

            # TODO: normalize out commissions details?
            details = {
                'contract': asdict(fill.contract),
                'execution': asdict(fill.execution),
                'commissions': asdict(fill.commissionReport),
                'broker_time': execu.time,   # supposedly server fill time
                'name': 'ib',
            }

            msg = BrokerdFill(
                # should match the value returned from `.submit_limit()`
                reqid=execu.orderId,
                time_ns=time.time_ns(),  # cuz why not

                action=action_map[execu.side],
                size=execu.shares,
                price=execu.price,

                broker_details=details,
                # XXX: required by order mode currently
                broker_time=details['broker_time'],

            )

        elif event_name == 'error':

            err: dict = item

            # f$#$% gawd dammit insync..
            con = err['contract']
            if isinstance(con, Contract):
                err['contract'] = asdict(con)

            if err['reqid'] == -1:
                log.error(f'TWS external order error:\n{pformat(err)}')

            # TODO: what schema for this msg if we're going to make it
            # portable across all backends?
            # msg = BrokerdError(**err)
            continue

        elif event_name == 'position':
            msg = pack_position(item)
            msg.account = accounts_def.inverse[msg.account]

        if getattr(msg, 'reqid', 0) < -1:

            # it's a trade event generated by TWS usage.
            log.info(f"TWS triggered trade\n{pformat(msg.dict())}")

            msg.reqid = 'tws-' + str(-1 * msg.reqid)

            # mark msg as from "external system"
            # TODO: probably something better then this.. and start
            # considering multiplayer/group trades tracking
            msg.broker_details['external_src'] = 'tws'
            continue

        # XXX: we always serialize to a dict for msgpack
        # translations, ideally we can move to an msgspec (or other)
        # encoder # that can be enabled in ``tractor`` ahead of
        # time so we can pass through the message types directly.
        await ems_stream.send(msg.dict())


@tractor.context
async def open_symbol_search(
    ctx: tractor.Context,

) -> None:
    # load all symbols locally for fast search
    await ctx.started({})

    async with ctx.open_stream() as stream:

        last = time.time()

        async for pattern in stream:
            log.debug(f'received {pattern}')
            now = time.time()

            assert pattern, 'IB can not accept blank search pattern'

            # throttle search requests to no faster then 1Hz
            diff = now - last
            if diff < 1.0:
                log.debug('throttle sleeping')
                await trio.sleep(diff)
                try:
                    pattern = stream.receive_nowait()
                except trio.WouldBlock:
                    pass

            if not pattern or pattern.isspace():
                log.warning('empty pattern received, skipping..')

                # TODO: *BUG* if nothing is returned here the client
                # side will cache a null set result and not showing
                # anything to the use on re-searches when this query
                # timed out. We probably need a special "timeout" msg
                # or something...

                # XXX: this unblocks the far end search task which may
                # hold up a multi-search nursery block
                await stream.send({})

                continue

            log.debug(f'searching for {pattern}')

            last = time.time()

            # async batch search using api stocks endpoint and module
            # defined adhoc symbol set.
            stock_results = []

            async def stash_results(target: Awaitable[list]):
                stock_results.extend(await target)

            async with trio.open_nursery() as sn:
                sn.start_soon(
                    stash_results,
                    _trio_run_client_method(
                        method='search_stocks',
                        pattern=pattern,
                        upto=5,
                    )
                )

                # trigger async request
                await trio.sleep(0)

                # match against our ad-hoc set immediately
                adhoc_matches = fuzzy.extractBests(
                    pattern,
                    list(_adhoc_futes_set),
                    score_cutoff=90,
                )
                log.info(f'fuzzy matched adhocs: {adhoc_matches}')
                adhoc_match_results = {}
                if adhoc_matches:
                    # TODO: do we need to pull contract details?
                    adhoc_match_results = {i[0]: {} for i in adhoc_matches}

            log.debug(f'fuzzy matching stocks {stock_results}')
            stock_matches = fuzzy.extractBests(
                pattern,
                stock_results,
                score_cutoff=50,
            )

            matches = adhoc_match_results | {
                item[0]: {} for item in stock_matches
            }
            # TODO: we used to deliver contract details
            # {item[2]: item[0] for item in stock_matches}

            log.debug(f"sending matches: {matches.keys()}")
            await stream.send(matches)
