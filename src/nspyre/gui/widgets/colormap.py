import time
from functools import partial
from typing import Any
from typing import Dict

import numpy as np
from pyqtgraph import ImageView
from pyqtgraph import PlotItem
from pyqtgraph.colormap import getFromMatplotlib
from pyqtgraph.Qt import QtCore
from pyqtgraph.Qt import QtGui
from pyqtgraph.Qt import QtWidgets

from ..style._style import nspyre_font
from .update_loop import UpdateLoop


class ColorMapWidget(QtWidgets.QWidget):
    """Qt widget for displaying 2D data using pyqtgraph ImageView."""

    new_data = QtCore.Signal()
    """Qt Signal emitted when new data is available."""

    def __init__(
        self,
        *args,
        title: str = '',
        btm_label: str = '',
        lft_label: str = '',
        colormap=None,
        font: QtGui.QFont = nspyre_font,
        **kwargs,
    ):
        """
        Args:
            title: Plot title.
            btm_label: Plot bottom axis label.
            lft_label: Plot left axis label.
            colormap: pyqtgraph `ColorMap <https://pyqtgraph.readthedocs.io/en/\
                latest/api_reference/colormap.html#pyqtgraph.ColorMap>`__ object.
            font: Font to use in the plot title, axis labels, etc., although
                the font type may not be fully honored.
        """
        super().__init__(*args, **kwargs)

        if colormap is None:
            colormap = getFromMatplotlib('magma')

        # layout for storing plot
        self.layout = QtWidgets.QVBoxLayout()

        # pyqtgraph widget for displaying an Image (2d or 3d plot) and related
        # items like axes, legends, etc.
        self.plot_item = PlotItem()
        self.image_view = ImageView(view=self.plot_item)
        self.layout.addWidget(self.image_view)

        # plot settings
        self.plot_item.setTitle(title, size=f'{font.pointSize()}pt')
        self.plot_item.enableAutoRange(True)

        # colormap
        self.image_view.setColorMap(colormap)

        # axes
        self.btm_axis = self.plot_item.getAxis('bottom')
        self.btm_axis.setLabel(text=btm_label)
        self.btm_axis.label.setFont(font)
        self.btm_axis.setTickFont(font)
        self.btm_axis.enableAutoSIPrefix(False)
        self.lft_axis = self.plot_item.getAxis('left')
        self.lft_axis.setLabel(text=lft_label)
        self.lft_axis.label.setFont(font)
        self.lft_axis.setTickFont(font)
        self.lft_axis.enableAutoSIPrefix(False)

        # we keep a dict containing the x-axis, y-axis, z-axis (optional, only
        # for 3D images), data, semaphore, and pyqtgraph PlotDataItem
        # associated with each line plot
        self.image: Dict[str, Any] = {
            'x': [],
            'y': [],
            'z': None,
            'data': [],
            'sem': QtCore.QSemaphore(n=1),
        }

        self.setLayout(self.layout)

        # TODO
        self.destroyed.connect(partial(self._stop))

        # Plot setup code
        self.setup()

        # thread for updating the plot data
        self.update_loop = UpdateLoop(self.update)
        # process new data when a signal is generated by the update thread
        self.new_data.connect(self._process_data)
        # start the thread
        self.update_loop.start()

    def _stop(self):
        """ """
        self.update_loop.stop()
        # TODO teardown

    def _process_data(self):
        """Update the color map triggered by set_data."""
        try:
            if self.image['z'] is None:
                axes = {'x': 0, 'y': 1}
            else:
                axes = {'x': 0, 'y': 1, 't': 2}
            z_index = self.image_view.currentIndex
            xs, ys = self.image['x'], self.image['y']
            x_range, y_range = xs[-1] - xs[0], ys[-1] - ys[0]
            x_pos, y_pos = np.mean(xs) - x_range / 2, np.mean(ys) - y_range / 2
            self.image_view.setImage(
                self.image['data'],
                pos=[x_pos, y_pos],
                scale=[x_range / len(xs), y_range / len(ys)],
                autoRange=False,
                autoLevels=False,
                autoHistogramRange=False,
                axes=axes,
                levelMode='mono',
                xvals=self.image['z'],
            )
            self.image_view.setCurrentIndex(z_index)
        except Exception as exc:
            raise exc
        finally:
            self.image['sem'].release()

    def setup(self):
        """Subclasses should override this function to perform any setup code."""
        pass

    def update(self):
        """Subclasses should override this function to update the plot. This
        function will be run in a separate Thread."""
        time.sleep(1)

    def teardown(self):
        """Subclasses should override this function to perform any teardown code."""
        pass

    def set_data(self, xs, ys, data, zs=None):
        """Queue up x,y,z and data to update the color map. Threadsafe.

        Args:
            name: Name of the plot.
            xs: Array-like of data for the x-axis.
            ys: Array-like of data for the y-axis.
            data: TODO - Jacob wuz here.
            zs: Optional array-like of data for the z-axis.
        Raises:
            ValueError: An error with the supplied arguments.
        """
        # block until any previous calls to set_data have been fully processed
        self.image['sem'].acquire()
        # set the new x and y data
        self.image['x'] = xs
        self.image['y'] = ys
        if zs is not None:
            self.image['z'] = zs
        self.image['data'] = data
        # notify the watcher
        try:
            self.parent()
        except RuntimeError:
            # this Qt object has already been deleted
            return
        else:
            # notify that new data is available
            self.new_data.emit()
