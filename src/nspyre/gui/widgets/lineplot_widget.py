"""
A wrapper for pyqtgraph PlotWidget.

Copyright (c) 2021, Jacob Feder
All rights reserved.

This work is licensed under the terms of the 3-Clause BSD license.
For a copy, see <https://opensource.org/licenses/BSD-3-Clause>.
"""
import logging
import time
from typing import Any
from typing import Dict

import pyqtgraph as pg
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtCore import QSemaphore
from PyQt5.QtCore import QThread
from PyQt5.QtGui import QColor
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QVBoxLayout
from PyQt5.QtWidgets import QWidget

from ..style.colors import colors
from ..style.colors import cyclic_colors
from ..style.style import nspyre_font

logger = logging.getLogger(__name__)


class WidgetUpdateThread(QThread):
    """Run update_func() repeatedly in a thread."""

    def __init__(self, update_func, report_fps=False, fps_period=1):
        """TODO"""
        super().__init__()
        self.update_func = update_func
        self.report_fps = report_fps
        self.fps_period = fps_period

    def run(self):
        """Thread entry point"""
        # keep track of how frequently update_func is called in the fps_period
        fps_counter = 0
        # time since the last reporting of the plot update FPS
        last_fps = time.time()
        while self.update_func:
            self.update_func()
            # calculate how many times per second update_func is being called
            if self.report_fps:
                fps_counter += 1
                now = time.time()
                # time difference since last FPS report
                td = now - last_fps
                if td > self.fps_period:
                    fps = fps_counter / td
                    logger.debug(f'plotting FPS: {fps:0.3f}')
                    last_fps = now
                    fps_counter = 0


class LinePlotWidget(QWidget):
    """Qt widget that generates a pyqtgraph 1D line plot with some reasonable default settings and a variety of added features.
    TODO: example
    """

    new_data = pyqtSignal(str)

    def __init__(
        self,
        *args,
        title: str = '',
        xlabel: str = '',
        ylabel: str = '',
        font: QFont = nspyre_font,
        **kwargs,
    ):
        """Initialize a LinePlotWidget.

        Args:
            title: Plot title.
            xlabel: Plot x-axis label.
            ylabel: Plot y-axis label.
            font: Font to use in the plot title, axis labels, etc., although the font type may not be fully honored.
        """
        super().__init__(*args, **kwargs)

        # layout for storing plot
        self.layout = QVBoxLayout()

        # pyqtgraph widget for displaying a plot and related
        # items like axes, legends, etc.
        self.plot_widget = pg.PlotWidget()
        self.layout.addWidget(self.plot_widget)

        # plot settings
        self.plot_widget.setTitle(title, size=f'{font.pointSize()}pt')
        self.plot_widget.enableAutoRange(True)
        # colors
        self.current_color_idx = 0
        self.plot_widget.setBackground(colors['black'])
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)

        # axes
        self.xaxis = self.plot_widget.getAxis('bottom')
        self.xaxis.setLabel(text=xlabel)
        self.xaxis.label.setFont(font)
        self.xaxis.setTickFont(font)
        self.xaxis.enableAutoSIPrefix(False)
        self.yaxis = self.plot_widget.getAxis('left')
        self.yaxis.setLabel(text=ylabel)
        self.yaxis.label.setFont(font)
        self.yaxis.setTickFont(font)
        self.yaxis.enableAutoSIPrefix(False)

        # legend
        self.plot_widget.addLegend(labelTextSize=f'{font.pointSize()}pt')

        # a dict mapping data set names (str) and a sub-dict containing the x data, y data, semaphore, and pyqtgraph PlotDataItem associated with each line plot
        self.plots: Dict[str, Dict[str, Any]] = {}

        self.setLayout(self.layout)

        # plot setup code
        self.setup()

        # thread for updating the plot data
        self.update_thread = WidgetUpdateThread(self.update)
        # process new data when a signal is generated by the update thread
        self.new_data.connect(self._process_data)
        # start the thread
        self.update_thread.start()

    def setup(self):
        """Subclasses should override this function to perform any setup code"""
        pass

    def update(self):
        """Subclasses should override this function to update the plot. This function will be run in a separate Thread."""
        time.sleep(1)

    def teardown(self):
        """Subclasses should override this function to perform any teardown code"""
        pass

    def _next_color(self):
        """Cycle through a set of colors"""
        idx = self.current_color_idx % len(cyclic_colors)
        color = pg.mkColor(cyclic_colors[idx])
        self.current_color_idx += 1
        return color

    def new_plot(
        self,
        name: str,
        pen: QColor = None,
        symbolBrush=(255, 255, 255, 100),
        symbolPen=(255, 255, 255, 100),
        symbol: str = 's',
        symbolSize: int = 5,
    ):
        """Add a new plot to the PlotWidget.

        Args:
            name: Name of the plot.
            pen: See https://pyqtgraph.readthedocs.io/en/latest/graphicsItems/plotdataitem.html.
            symbolBrush: See https://pyqtgraph.readthedocs.io/en/latest/graphicsItems/plotdataitem.html.
            symbolPen: See https://pyqtgraph.readthedocs.io/en/latest/graphicsItems/plotdataitem.html.
            symbol: See https://pyqtgraph.readthedocs.io/en/latest/graphicsItems/plotdataitem.html.
            symbolSize: See https://pyqtgraph.readthedocs.io/en/latest/graphicsItems/plotdataitem.html.

        Raises:
            ValueError: An error with the supplied arguments.
        """
        if name in self.plots:
            raise ValueError(f'A plot with the name {name} already exists.')

        if not pen:
            pen = self._next_color()

        # create pyqtgraph PlotDataItem
        plt = self.plot_widget.plot(
            pen=pen,
            symbolBrush=symbolBrush,
            symbolPen=symbolPen,
            symbol=symbol,
            symbolSize=symbolSize,
            name=name,
        )
        self.plots[name] = {'x': [], 'y': [], 'plot': plt, 'sem': QSemaphore(n=1)}

    def set_data(self, name: str, xdata, ydata):
        """Queue up x/y data to update a line plot. Threadsafe.

        Args:
            name: Name of the plot.
            xdata: array-like of data for the x-axis.
            ydata: array-like of data for the y-axis.

        Raises:
            ValueError: An error with the supplied arguments.
        """
        if name not in self.plots:
            raise ValueError(f'A plot with the name {name} does not exist.')

        # block until any previous calls to set_data have been fully processed
        self.plots[name]['sem'].acquire()
        # set the new x and y data
        self.plots[name]['x'] = xdata
        self.plots[name]['y'] = ydata
        # notify the watcher
        try:
            self.parent()
        except RuntimeError:
            # this Qt object has already been deleted
            return
        else:
            # notify that new data is available
            self.new_data.emit(name)

    def _process_data(self, name):
        """Update a line plot triggered by set_data."""
        try:
            self.plots[name]['plot'].setData(
                self.plots[name]['x'], self.plots[name]['y']
            )
        except Exception as exc:
            raise exc
        finally:
            self.plots[name]['sem'].release()

    def add_zoom_region(self):
        """Create a GUI element for selecting a plot subregion. Returns a new PlotWidget that contains a view with it's x span linked to the area selected by the plot subregion."""
        # current display region
        plot_xrange, plot_yrange = self.plot_widget.viewRange()
        xmin, xmax = plot_xrange
        center = (xmax + xmin) / 2
        span = (xmax - xmin) / 20
        # create GUI element for subregion selection
        linear_region = pg.LinearRegionItem(values=[center - span, center + span])
        self.plot_widget.addItem(linear_region)

        # TODO
        # p9 = win.addPlot(title="Zoom on selected region")
        # p9.plot(data2)
        # def updatePlot():
        #     p9.setXRange(*lr.getRegion(), padding=0)
        # def updateRegion():
        #     lr.setRegion(p9.getViewBox().viewRange()[0])
        # lr.sigRegionChanged.connect(updatePlot)
        # p9.sigXRangeChanged.connect(updateRegion)
        # updatePlot()

    def stop(self):
        self.update_thread.update_func = None
        self.teardown()
