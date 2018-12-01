"""
monitor: a real-time, sorted watchlist.

Launch with ``piker monitor <watchlist name>``.

(Currently there's a bunch of questrade specific stuff in here)
"""
from itertools import chain
from types import ModuleType, AsyncGeneratorType
from typing import List

import trio
import tractor
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.stacklayout import StackLayout
from kivy.uix.button import Button
from kivy.lang import Builder
from kivy import utils
from kivy.app import async_runTouchApp
from kivy.core.window import Window
from async_generator import aclosing

from ..log import get_logger
from .pager import PagerView
from .kivy.hoverable import HoverBehavior

log = get_logger('monitor')


_colors2hexs = {
    'darkgray': 'a9a9a9',
    'gray': '808080',
    'green': '008000',
    'forestgreen': '228b22',
    'red2': 'ff3333',
    'red': 'ff0000',
    'firebrick': 'b22222',
}

_colors = {key: utils.rgba(val) for key, val in _colors2hexs.items()}


def colorcode(name):
    return _colors[name if name else 'gray']


_bs = 0.75  # border size

# medium shade of gray that seems to match the
# default i3 window borders
_i3_rgba = [0.14]*3 + [1]

# slightly off black like the jellybean bg from
# vim colorscheme
_cell_rgba = [0.07]*3 + [1]
_black_rgba = [0]*4

_kv = (f'''
#:kivy 1.10.0

<Cell>
    font_size: 21
    # make text wrap to botom
    text_size: self.size
    halign: 'center'
    valign: 'middle'
    size: self.texture_size
    # color: {colorcode('gray')}
    # font_color: {colorcode('gray')}
    font_name: 'Roboto-Regular'
    background_color: [0]*4  # by default transparent; use row color
    # background_color: {_cell_rgba}
    # spacing: 0, 0
    # padding: [0]*4

<HeaderCell>
    font_size: 21
    background_color: [0]*4  # by default transparent; use row color
    # background_color: {_cell_rgba}
    # canvas.before:
    #     Color:
    #         rgba: [0.13]*4
    #     BorderImage:  # use a fixed size border
    #         pos: self.pos
    #         size: [self.size[0] - {_bs}, self.size[1]]
    #         # 0s are because the containing TickerTable already has spacing
    #         # border: [0, {_bs} , 0, {_bs}]
    #         border: [0, {_bs} , 0, 0]

<TickerTable>
    spacing: [{_bs}]
    # row_force_default: True
    row_default_height: 62
    cols: 1
    canvas.before:
        Color:
            # i3 style gray as background
            rgba: {_i3_rgba}
            # rgba: {_cell_rgba}
        Rectangle:
            # scale with container self here refers to the widget i.e BoxLayout
            pos: self.pos
            size: self.size

<BidAskLayout>
    spacing: [{_bs}, 0]

<Row>
    # minimum_height: 200  # should be pulled from Cell text size
    # minimum_width: 200
    # row_force_default: True
    # row_default_height: 61  # determines the header row size
    padding: [0]*4
    spacing: [0]
    canvas.before:
        Color:
            # rgba: [0]*4
            rgba: {_cell_rgba}
        Rectangle:
            # self here refers to the widget i.e Row(GridLayout)
            pos: self.pos
            size: self.size
        # row higlighting on mouse over
        Color:
            rgba: {_i3_rgba}
        RoundedRectangle:
            size: self.width, self.height if self.hovered else 1
            pos: self.pos
            radius: (10,)



# part of the `PagerView`
<SearchBar>
    size_hint: 1, None
    # static size of 51 px
    height: 51
    font_size: 25
    background_color: {_i3_rgba}
''')


class Cell(Button):
    """Data cell: the fundemental widget.

    ``key`` is the column name index value.
    """
    def __init__(self, key=None, **kwargs):
        super(Cell, self).__init__(**kwargs)
        self.key = key


class HeaderCell(Cell):
    """Column header cell label.
    """
    def on_press(self, value=None):
        """Clicking on a col header indicates to sort rows by this column
        in `update_quotes()`.
        """
        table = self.row.table
        # if this is a row header cell then sort by the clicked field
        if self.row.is_header:
            table.sort_key = self.key

            last = table.last_clicked_col_cell
            if last and last is not self:
                last.underline = False
                last.bold = False

            # outline the header text to indicate it's been the last clicked
            self.underline = True
            self.bold = True
            # mark this cell as the last selected
            table.last_clicked_col_cell = self
            # sort and render the rows immediately
            self.row.table.render_rows(table.quote_cache)

        # allow highlighting of row headers for tracking
        elif self.is_header:
            if self.background_color == self.color:
                self.background_color = _black_rgba
            else:
                self.background_color = self.color


class BidAskLayout(StackLayout):
    """Cell which houses three buttons containing a last, bid, and ask in a
    single unit oriented with the last 2 under the first.
    """
    def __init__(self, values, header=False, **kwargs):
        # uncomment to get vertical stacked bid-ask
        # super(BidAskLayout, self).__init__(orientation='bt-lr', **kwargs)
        super(BidAskLayout, self).__init__(orientation='lr-tb', **kwargs)
        assert len(values) == 3, "You can only provide 3 values: last,bid,ask"
        self._keys2cells = {}
        cell_type = HeaderCell if header else Cell
        top_size = cell_type().font_size
        small_size = top_size - 4
        top_prop = 0.5  # proportion of size used by top cell
        bottom_prop = 1 - top_prop
        for (key, size_hint, font_size), value in zip(
            [('last', (1, top_prop), top_size),
             ('bid', (0.5, bottom_prop), small_size),
             ('ask', (0.5, bottom_prop), small_size)],
            # uncomment to get vertical stacked bid-ask
            # [('last', (top_prop, 1), top_size),
            #  ('bid', (bottom_prop, 0.5), small_size),
            #  ('ask', (bottom_prop, 0.5), small_size)],
            values
        ):
            cell = cell_type(
                text=str(value),
                size_hint=size_hint,
                # width=self.width/2 - 3,
                font_size=font_size
            )
            self._keys2cells[key] = cell
            cell.key = value
            cell.is_header = header
            setattr(self, key, cell)
            self.add_widget(cell)

        # should be assigned by referrer
        self.row = None

    def get_cell(self, key):
        return self._keys2cells[key]

    @property
    def row(self):
        return self.row

    @row.setter
    def row(self, row):
        # so hideous
        for cell in self.cells:
            cell.row = row

    @property
    def cells(self):
        return [self.last, self.bid, self.ask]


class Row(GridLayout, HoverBehavior):
    """A grid for displaying a row of ticker quote data.

    The row fields can be updated using the ``fields`` property which will in
    turn adjust the text color of the values based on content changes.
    """
    def __init__(
        self, record, headers=(), bidasks=None, table=None,
        is_header=False,
        **kwargs
    ):
        super(Row, self).__init__(cols=len(record), **kwargs)
        self._cell_widgets = {}
        self._last_record = record
        self.table = table
        self.is_header = is_header

        # selection state
        self.mouse_over = False

        # create `BidAskCells` first
        layouts = {}
        bidasks = bidasks or {}
        ba_cells = {}
        for key, children in bidasks.items():
            layout = BidAskLayout(
                [record[key]] + [record[child] for child in children],
                header=is_header
            )
            layout.row = self
            layouts[key] = layout
            for i, child in enumerate([key] + children):
                ba_cells[child] = layout.cells[i]

        children_flat = list(chain.from_iterable(bidasks.values()))
        self._cell_widgets.update(ba_cells)

        # build out row using Cell labels
        for (key, val) in record.items():
            header = key in headers

            # handle bidask cells
            if key in layouts:
                self.add_widget(layouts[key])
            elif key in children_flat:
                # these cells have already been added to the `BidAskLayout`
                continue
            else:
                cell = self._append_cell(val, key, header=header)
                cell.key = key
                self._cell_widgets[key] = cell

    def get_cell(self, key):
        return self._cell_widgets[key]

    def _append_cell(self, text, key, header=False):
        if not len(self._cell_widgets) < self.cols:
            raise ValueError(f"Can not append more then {self.cols} cells")

        # header cells just have a different colour
        celltype = HeaderCell if header else Cell
        cell = celltype(text=str(text), key=key)
        cell.is_header = header
        cell.row = self
        self.add_widget(cell)
        return cell

    def update(self, record, displayable):
        """Update this row's cells with new values from a quote ``record``.

        Return all cells that changed in a ``dict``.
        """
        # color changed field values
        cells = {}
        gray = colorcode('gray')
        fgreen = colorcode('forestgreen')
        red = colorcode('red2')
        for key, val in record.items():
            # logic for cell text coloring: up-green, down-red
            if self._last_record[key] < val:
                color = fgreen
            elif self._last_record[key] > val:
                color = red
            else:
                color = gray

            cell = self.get_cell(key)
            cell.text = str(displayable[key])
            cell.color = color
            if color != gray:
                cells[key] = cell

        self._last_record = record
        return cells

    # mouse over handlers
    def on_enter(self):
        """Highlight layout on enter.
        """
        log.debug(
            f"Entered row {type(self)} through {self.border_point}")
        # don't highlight header row
        if getattr(self, 'is_header', None):
            self.hovered = False

    def on_leave(self):
        """Un-highlight layout on exit.
        """
        log.debug(
            f"Left row {type(self)} through {self.border_point}")


class TickerTable(GridLayout):
    """A grid for displaying ticker quote records as a table.
    """
    def __init__(self, sort_key='%', quote_cache={}, **kwargs):
        super(TickerTable, self).__init__(**kwargs)
        self.symbols2rows = {}
        self.sort_key = sort_key
        self.quote_cache = quote_cache
        self.row_filter = lambda item: item
        # for tracking last clicked column header cell
        self.last_clicked_col_cell = None
        self._last_row_toggle = 0

    def append_row(self, record, bidasks=None):
        """Append a `Row` of `Cell` objects to this table.
        """
        row = Row(record, headers=('symbol',), bidasks=bidasks, table=self)
        # store ref to each row
        self.symbols2rows[row._last_record['symbol']] = row
        self.add_widget(row)
        return row

    def render_rows(
            self, pairs: {str: (dict, Row)}, sort_key: str = None,
            row_filter=None,
    ):
        """Sort and render all rows on the ticker grid from ``pairs``.
        """
        self.clear_widgets()
        sort_key = sort_key or self.sort_key
        for data, row in filter(
            row_filter or self.row_filter,
                reversed(
                    sorted(pairs.values(), key=lambda item: item[0][sort_key])
                )
        ):
            self.add_widget(row)  # row append

    def ticker_search(self, patt):
        """Return sequence of matches when pattern ``patt`` is in a
        symbol name. Most naive algo possible for the moment.
        """
        for symbol, row in self.symbols2rows.items():
            if patt in symbol:
                yield symbol, row

    def search(self, patt):
        """Search bar api compat.
        """
        return dict(self.ticker_search(patt)) or {}


async def update_quotes(
    nursery: trio._core._run.Nursery,
    brokermod: ModuleType,
    widgets: dict,
    agen: AsyncGeneratorType,
    symbol_data: dict,
    first_quotes: dict
):
    """Process live quotes by updating ticker rows.
    """
    table = widgets['table']
    flash_keys = {'low', 'high'}

    async def revert_cells_color(cells):
        await trio.sleep(0.3)
        for cell in cells:
            cell.background_color = _black_rgba

    def color_row(row, data, cells):
        hdrcell = row.get_cell('symbol')
        chngcell = row.get_cell('%')

        # determine daily change color
        daychange = float(data['%'])
        if daychange < 0.:
            color = colorcode('red2')
        elif daychange > 0.:
            color = colorcode('forestgreen')
        else:
            color = colorcode('gray')

        # update row header and '%' cell text color
        chngcell.color = hdrcell.color = color
        # if the cell has been "highlighted" make sure to change its color
        if hdrcell.background_color != [0]*4:
            hdrcell.background_color = color

        # briefly highlight bg of certain cells on each trade execution
        unflash = set()
        tick_color = None
        last = cells.get('last')
        if not last:
            vol = cells.get('vol')
            if not vol:
                return  # no trade exec took place

            # flash gray on volume tick
            # (means trade exec @ current price)
            last = row.get_cell('last')
            tick_color = colorcode('gray')
        else:
            tick_color = last.color

        last.background_color = tick_color
        unflash.add(last)
        # flash the size cell
        size = row.get_cell('size')
        size.background_color = tick_color
        unflash.add(size)

        # flash all other cells
        for key in flash_keys:
            cell = cells.get(key)
            if cell:
                cell.background_color = cell.color
                unflash.add(cell)

        # revert flash state momentarily
        nursery.start_soon(revert_cells_color, unflash)

    cache = {}
    table.quote_cache = cache

    # initial coloring
    for sym, quote in first_quotes.items():
        row = table.symbols2rows[sym]
        record, displayable = brokermod.format_quote(
            quote, symbol_data=symbol_data)
        row.update(record, displayable)
        color_row(row, record, {})
        cache[sym] = (record, row)

    # render all rows once up front
    table.render_rows(cache)

    # real-time cell update loop
    async for quotes in agen:  # new quotes data only
        for symbol, quote in quotes.items():
            record, displayable = brokermod.format_quote(
                quote, symbol_data=symbol_data)
            row = table.symbols2rows[symbol]
            cache[symbol] = (record, row)
            cells = row.update(record, displayable)
            color_row(row, record, cells)

        table.render_rows(cache)
        log.debug("Waiting on quotes")

    log.warn("Data feed connection dropped")
    nursery.cancel_scope.cancel()


async def _async_main(
    name: str,
    portal: tractor._portal.Portal,
    tickers: List[str],
    brokermod: ModuleType,
    rate: int,
    test: bool = False
) -> None:
    '''Launch kivy app + all other related tasks.

    This is started with cli cmd `piker monitor`.
    '''
    if test:
        # stream from a local test file
        quote_gen = await portal.run(
            "piker.brokers.data", 'stream_from_file',
            filename=test
        )
    else:
        # start live streaming from broker daemon
        quote_gen = await portal.run(
            "piker.brokers.data", 'start_quote_stream',
            broker=brokermod.name, symbols=tickers)

    # subscribe for tickers (this performs a possible filtering
    # where invalid symbols are discarded)
    sd = await portal.run(
        "piker.brokers.data", 'symbol_data',
        broker=brokermod.name, tickers=tickers)

    async with trio.open_nursery() as nursery:
        # get first quotes response
        log.debug("Waiting on first quote...")
        quotes = await quote_gen.__anext__()
        first_quotes = [
            brokermod.format_quote(quote, symbol_data=sd)[0]
            for quote in quotes.values()]

        if first_quotes[0].get('last') is None:
            log.error("Broker API is down temporarily")
            nursery.cancel_scope.cancel()
            return

        # build out UI
        Window.set_title(f"monitor: {name}\t(press ? for help)")
        Builder.load_string(_kv)
        box = BoxLayout(orientation='vertical', spacing=0)

        # define bid-ask "stacked" cells
        # (TODO: needs some rethinking and renaming for sure)
        bidasks = brokermod._bidasks

        # add header row
        headers = first_quotes[0].keys()
        header = Row(
            {key: key for key in headers},
            headers=headers,
            bidasks=bidasks,
            is_header=True,
            size_hint=(1, None),
        )
        box.add_widget(header)

        # build table
        table = TickerTable(
            cols=1,
            size_hint=(1, None),
        )
        for ticker_record in first_quotes:
            table.append_row(ticker_record, bidasks=bidasks)
        # associate the col headers row with the ticker table even though
        # they're technically wrapped separately in containing BoxLayout
        header.table = table

        # mark the initial sorted column header as bold and underlined
        sort_cell = header.get_cell(table.sort_key)
        sort_cell.bold = sort_cell.underline = True
        table.last_clicked_col_cell = sort_cell

        # set up a pager view for large ticker lists
        table.bind(minimum_height=table.setter('height'))
        pager = PagerView(box, table, nursery)
        box.add_widget(pager)

        widgets = {
            # 'anchor': anchor,
            'root': box,
            'table': table,
            'box': box,
            'header': header,
            'pager': pager,
        }
        nursery.start_soon(
            update_quotes, nursery, brokermod, widgets, quote_gen, sd, quotes)

        try:
            # Trio-kivy entry point.
            await async_runTouchApp(widgets['root'])  # run kivy
        finally:
            await quote_gen.aclose()  # cancel aysnc gen call
            # un-subscribe from symbols stream (cancel if brokerd
            # was already torn down - say by SIGINT)
            with trio.move_on_after(0.2):
                await portal.run(
                    "piker.brokers.data", 'modify_quote_stream',
                    broker=brokermod.name,
                    feed_type='stock',
                    symbols=[]
                )

            # cancel GUI update task
            nursery.cancel_scope.cancel()
