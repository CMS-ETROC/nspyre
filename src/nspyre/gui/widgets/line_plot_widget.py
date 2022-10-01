"""
A wrapper for pyqtgraph PlotWidget.

Copyright (c) 2022, Jacob Feder
All rights reserved.

This work is licensed under the terms of the 3-Clause BSD license.
For a copy, see <https://opensource.org/licenses/BSD-3-Clause>.
"""
import logging
import time
from threading import Lock
from typing import Any
from typing import Dict

import numpy as np
from pyqtgraph import SpinBox, PlotWidget, mkColor, LinearRegionItem
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtCore import QSemaphore
from PyQt5.QtGui import QColor
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QHBoxLayout
from PyQt5.QtWidgets import QVBoxLayout
from PyQt5.QtWidgets import QWidget
from PyQt5.QtWidgets import QLineEdit
from PyQt5.QtWidgets import QPushButton
from PyQt5.QtWidgets import QLabel
from nspyre import DataSink

from ..style.colors import colors
from ..style.colors import cyclic_colors
from ..style.style import nspyre_font
from .widget_update_thread import WidgetUpdateThread

logger = logging.getLogger(__name__)


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
        legend: bool = True,
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

        self.font = font

        # layout for storing plot
        self.layout = QVBoxLayout()

        # pyqtgraph widget for displaying a plot and related
        # items like axes, legends, etc.
        self.plot_widget = PlotWidget()
        self.layout.addWidget(self.plot_widget)

        # plot settings
        self.set_title(title)
        self.plot_widget.enableAutoRange(True)
        # colors
        self.current_color_idx = 0
        self.plot_widget.setBackground(colors['black'])
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)

        # x axis
        self.xaxis = self.plot_widget.getAxis('bottom')
        self.xaxis.setLabel(text=xlabel)
        self.xaxis.label.setFont(font)
        self.xaxis.setTickFont(font)
        self.xaxis.enableAutoSIPrefix(False)
        # y axis
        self.yaxis = self.plot_widget.getAxis('left')
        self.yaxis.setLabel(text=ylabel)
        self.yaxis.label.setFont(font)
        self.yaxis.setTickFont(font)
        self.yaxis.enableAutoSIPrefix(False)

        if legend:
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

    def set_title(self, title):
        self.plot_widget.setTitle(title, size=f'{self.font.pointSize()}pt')

    def setup(self):
        """Subclasses should override this function to perform any setup code"""
        pass

    def update(self):
        """Subclasses should override this function to update the plot. This function will be run in a separate QThread."""
        time.sleep(1)

    def teardown(self):
        """Subclasses should override this function to perform any teardown code"""
        pass

    def _next_color(self):
        """Cycle through a set of colors"""
        idx = self.current_color_idx % len(cyclic_colors)
        color = mkColor(cyclic_colors[idx])
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
        linear_region = LinearRegionItem(values=[center - span, center + span])
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


class FlexSinkLinePlotWidget(QWidget):
    """QWidget that allows the user to connect to an arbitrary nspyre DataSource and plot its data. 
    The DataSource should contain the following attributes:
        title: plot title string
        xlabel: x label string
        ylabel: y label string
        datasets: dictionary where keys are a dataset name to display in the 
            legend, and values are data as a 2D numpy array like 
            np.array([[xdata0, xdata1, ...], [ydata0, ydata1, ...]]), or a 
            list of a such numpy arrays which will be concatenated
    """
    def __init__(self):
        super().__init__()

        layout = QVBoxLayout()

        # lineedit and button for selecting the data source
        datasource_layout = QHBoxLayout()
        self.datasource_lineedit = QLineEdit()
        self.update_button = QPushButton('Connect')
        self.update_button.clicked.connect(self.update_source)
        datasource_layout.addWidget(self.update_button)
        datasource_layout.addWidget(self.datasource_lineedit)

        # lineplot widget
        self.lineplot = _FlexSinkLinePlotWidget()

        npoints_layout = QHBoxLayout()
        npoints_layout.addWidget(QLabel('Numer of Points'))
        # spinbox for entering the number of points to plot
        self.npoints_spinbox = SpinBox(value=0, int=True, bounds=(0, None))
        def user_changed_npoints(spinbox):
            self.lineplot.npoints = spinbox.value()
        self.npoints_spinbox.sigValueChanged.connect(user_changed_npoints)
        npoints_layout.addWidget(self.npoints_spinbox)

        layout.addLayout(datasource_layout)
        layout.addWidget(self.lineplot)
        layout.addLayout(npoints_layout)

        self.setLayout(layout)

    def update_source(self):
        self.lineplot.new_source(self.datasource_lineedit.text())


class _FlexSinkLinePlotWidget(LinePlotWidget):
    def __init__(self):
        super().__init__()
        self.sink = None
        # mutex for protecting access to the data sink
        self.mutex = Lock()
        # number of points to plot
        self.npoints = 0

    def new_source(self, data_source_name, timeout=1):
        with self.mutex:
            self.teardown()
            self.data_source_name = data_source_name
            self.sink = DataSink(data_source_name)

            # TODO
            # # clear previous plots
            # for p in self.plots:
            #     self.plots[p]['plot'].clear
            # self.plots = {}

            # try to get the plot title and x/y labels
            try:
                if self.sink.pop(timeout=timeout):
                    # set title
                    try:
                        title = self.sink.title
                    except AttributeError:
                        logger.info(f'Data source [{data_source_name}] has no "title" attribute - skipping')
                    else:
                        self.set_title(title)
                    # set xlabel
                    try:
                        xlabel = self.sink.xlabel
                    except AttributeError:
                        logger.info(f'Data source [{data_source_name}] has no "xlabel" attribute - skipping')
                    else:
                        self.xaxis.setLabel(text=xlabel)
                    # set ylabel
                    try:
                        ylabel = self.sink.ylabel
                    except AttributeError:
                        logger.info(f'Data source [{data_source_name}] has no "ylabel" attribute - skipping')
                    else:
                        self.yaxis.setLabel(text=ylabel)
                    try:
                        dsets = self.sink.datasets
                    except AttributeError:
                        logger.error(f'Data source [{data_source_name}] has no "datasets" attribute - exitting...')
                        raise RuntimeError
                    else:
                        if not isinstance(dsets, dict):
                            logger.error(f'Data source [{data_source_name}] "datasets" attribute is not a dictionary - exitting...')
                            raise RuntimeError
                        for d in dsets:
                            # make a new plot for each data set
                            self.new_plot(d)
                else:
                    # some other pop error occured
                    raise RuntimeError
            except (TimeoutError, RuntimeError):
                logger.error(f'Could not connect to new data source [{data_source_name}]')
                self.teardown()

    def teardown(self):
        if self.sink is not None:
            self.sink.stop()
            self.sink = None

    def update(self):
        with self.mutex:
            if self.sink is not None:
                if self.sink.pop():
                    for d in self.sink.datasets:
                        data = self.sink.datasets[d]
                        if isinstance(data, np.ndarray):
                            pass
                        elif isinstance(data, list) or isinstance(data, tuple):
                            # if the sink data is an array of numpy arrays, concatenate them
                            data = np.concatenate(data, axis=1)
                        # update the plot
                        if self.npoints != 0:
                            self.set_data(d, data[0][-self.npoints:], data[1][-self.npoints:])
                        else:
                            self.set_data(d, data[0], data[1])
            else:
                time.sleep(0.1)
