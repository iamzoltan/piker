"""
Console interface to UI components.
"""
from datetime import datetime
from functools import partial
import os
import click
import trio
import tractor

from ..cli import cli
from .. import watchlists as wl
from ..brokers.core import maybe_spawn_brokerd_as_subactor


_config_dir = click.get_app_dir('piker')
_watchlists_data_path = os.path.join(_config_dir, 'watchlists.json')


def _kivy_import_hack():
    # Command line hacks to make it work.
    # See the pkg mod.
    from .kivy import kivy  # noqa


@cli.command()
@click.option('--tl', is_flag=True, help='Enable tractor logging')
@click.option('--rate', '-r', default=3, help='Quote rate limit')
@click.option('--test', '-t', help='Test quote stream file')
@click.option('--dhost', '-dh', default='127.0.0.1',
              help='Daemon host address to connect to')
@click.argument('name', nargs=1, required=True)
@click.pass_obj
def monitor(config, rate, name, dhost, test, tl):
    """Start a real-time watchlist UI
    """
    # global opts
    brokermod = config['brokermod']
    loglevel = config['loglevel']
    log = config['log']

    watchlist_from_file = wl.ensure_watchlists(_watchlists_data_path)
    watchlists = wl.merge_watchlist(watchlist_from_file, wl._builtins)
    tickers = watchlists[name]
    if not tickers:
        log.error(f"No symbols found for watchlist `{name}`?")
        return

    _kivy_import_hack()
    from .monitor import _async_main

    async def main(tries):
        async with maybe_spawn_brokerd_as_subactor(
            tries=tries, loglevel=loglevel
        ) as portal:
            # run app "main"
            await _async_main(
                name, portal, tickers,
                brokermod, rate, test=test,
            )

    tractor.run(
        partial(main, tries=1),
        name='monitor',
        loglevel=loglevel if tl else None,
        rpc_module_paths=['piker.ui.monitor'],
        start_method='forkserver',
    )


@cli.command()
@click.option('--tl', is_flag=True, help='Enable tractor logging')
@click.option('--date', '-d', help='Contracts expiry date')
@click.option('--test', '-t', help='Test quote stream file')
@click.option('--rate', '-r', default=1, help='Logging level')
@click.argument('symbol', required=True)
@click.pass_obj
def optschain(config, symbol, date, tl, rate, test):
    """Start an option chain UI
    """
    # global opts
    loglevel = config['loglevel']
    brokername = config['broker']

    _kivy_import_hack()
    from .option_chain import _async_main

    async def main(tries):
        async with maybe_spawn_brokerd_as_subactor(
            tries=tries, loglevel=loglevel
        ):
            # run app "main"
            await _async_main(
                symbol,
                brokername,
                rate=rate,
                loglevel=loglevel,
                test=test,
            )

    tractor.run(
        partial(main, tries=1),
        name='kivy-options-chain',
        loglevel=loglevel if tl else None,
        start_method='forkserver',
    )


@cli.command()
@click.option('--tl', is_flag=True, help='Enable tractor logging')
@click.option('--date', '-d', help='Contracts expiry date')
@click.option('--test', '-t', help='Test quote stream file')
@click.option('--rate', '-r', default=1, help='Logging level')
@click.argument('symbol', required=True)
@click.pass_obj
def chart(config, symbol, date, tl, rate, test):
    """Start an option chain UI
    """
    from .qt._exec import run_qtrio
    from .qt._chart import QuotesTabWidget
    from .qt.quantdom.base import Symbol
    from .qt.quantdom.loaders import get_quotes

    async def plot_symbol(widgets):
        qtw = widgets['main']
        s = Symbol(ticker=symbol, mode=Symbol.SHARES)
        get_quotes(
            symbol=s.ticker,
            date_from=datetime(1900, 1, 1),
            date_to=datetime(2030, 12, 31),
        )
        # spawn chart
        qtw.update_chart(s)
        await trio.sleep_forever()

    run_qtrio(plot_symbol, (), QuotesTabWidget)
