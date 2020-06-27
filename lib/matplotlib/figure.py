"""
`matplotlib.figure` implements the following classes:

`Figure`
    Top level `~matplotlib.artist.Artist`, which holds all plot elements.

`SubplotParams`
    Control the default spacing between subplots.
"""

import inspect
import logging
from numbers import Integral

import numpy as np

import matplotlib as mpl
from matplotlib import docstring, projections
from matplotlib import __version__ as _mpl_version

import matplotlib.artist as martist
from matplotlib.artist import Artist, allow_rasterization
from matplotlib.backend_bases import (
    FigureCanvasBase, NonGuiException, MouseButton)
import matplotlib.cbook as cbook
import matplotlib.colorbar as cbar
import matplotlib.image as mimage

from matplotlib.axes import Axes, SubplotBase, subplot_class_factory
from matplotlib.blocking_input import BlockingMouseInput, BlockingKeyMouseInput
from matplotlib.gridspec import GridSpec, SubplotSpec
import matplotlib.legend as mlegend
from matplotlib.patches import Rectangle
from matplotlib.text import Text
from matplotlib.transforms import (Affine2D, Bbox, BboxTransformTo,
                                   TransformedBbox)
import matplotlib._layoutbox as layoutbox

_log = logging.getLogger(__name__)


def _stale_figure_callback(self, val):
    if self.figure:
        self.figure.stale = val


class _AxesStack(cbook.Stack):
    """
    Specialization of `.Stack`, to handle all tracking of `~.axes.Axes` in a
    `.Figure`.

    This stack stores ``key, (ind, axes)`` pairs, where:

    * **key** is a hash of the args and kwargs used in generating the Axes.
    * **ind** is a serial index tracking the order in which axes were added.

    AxesStack is a callable; calling it returns the current axes.
    The `current_key_axes` method returns the current key and associated axes.
    """

    def __init__(self):
        super().__init__()
        self._ind = 0

    def as_list(self):
        """
        Return a list of the Axes instances that have been added to the figure.
        """
        ia_list = [a for k, a in self._elements]
        ia_list.sort()
        return [a for i, a in ia_list]

    def get(self, key):
        """
        Return the Axes instance that was added with *key*.
        If it is not present, return *None*.
        """
        item = dict(self._elements).get(key)
        if item is None:
            return None
        cbook.warn_deprecated(
            "2.1",
            message="Adding an axes using the same arguments as a previous "
            "axes currently reuses the earlier instance.  In a future "
            "version, a new instance will always be created and returned.  "
            "Meanwhile, this warning can be suppressed, and the future "
            "behavior ensured, by passing a unique label to each axes "
            "instance.")
        return item[1]

    def _entry_from_axes(self, e):
        ind, k = {a: (ind, k) for k, (ind, a) in self._elements}[e]
        return (k, (ind, e))

    def remove(self, a):
        """Remove the axes from the stack."""
        super().remove(self._entry_from_axes(a))

    def bubble(self, a):
        """
        Move the given axes, which must already exist in the
        stack, to the top.
        """
        return super().bubble(self._entry_from_axes(a))

    def add(self, key, a):
        """
        Add Axes *a*, with key *key*, to the stack, and return the stack.

        If *key* is unhashable, replace it by a unique, arbitrary object.

        If *a* is already on the stack, don't add it again, but
        return *None*.
        """
        # All the error checking may be unnecessary; but this method
        # is called so seldom that the overhead is negligible.
        cbook._check_isinstance(Axes, a=a)
        try:
            hash(key)
        except TypeError:
            key = object()

        a_existing = self.get(key)
        if a_existing is not None:
            super().remove((key, a_existing))
            cbook._warn_external(
                "key {!r} already existed; Axes is being replaced".format(key))
            # I don't think the above should ever happen.

        if a in self:
            return None
        self._ind += 1
        return super().push((key, (self._ind, a)))

    def current_key_axes(self):
        """
        Return a tuple of ``(key, axes)`` for the active axes.

        If no axes exists on the stack, then returns ``(None, None)``.
        """
        if not len(self._elements):
            return self._default, self._default
        else:
            key, (index, axes) = self._elements[self._pos]
            return key, axes

    def __call__(self):
        return self.current_key_axes()[1]

    def __contains__(self, a):
        return a in self.as_list()


@cbook.deprecated("3.2")
class AxesStack(_AxesStack):
    pass


class SubplotParams:
    """
    A class to hold the parameters for a subplot.
    """
    def __init__(self, left=None, bottom=None, right=None, top=None,
                 wspace=None, hspace=None):
        """
        Defaults are given by :rc:`figure.subplot.[name]`.

        Parameters
        ----------
        left : float
            The position of the left edge of the subplots,
            as a fraction of the figure width.
        right : float
            The position of the right edge of the subplots,
            as a fraction of the figure width.
        bottom : float
            The position of the bottom edge of the subplots,
            as a fraction of the figure height.
        top : float
            The position of the top edge of the subplots,
            as a fraction of the figure height.
        wspace : float
            The width of the padding between subplots,
            as a fraction of the average axes width.
        hspace : float
            The height of the padding between subplots,
            as a fraction of the average axes height.
        """
        self.validate = True
        for key in ["left", "bottom", "right", "top", "wspace", "hspace"]:
            setattr(self, key, mpl.rcParams[f"figure.subplot.{key}"])
        self.update(left, bottom, right, top, wspace, hspace)

    def update(self, left=None, bottom=None, right=None, top=None,
               wspace=None, hspace=None):
        """
        Update the dimensions of the passed parameters. *None* means unchanged.
        """
        if self.validate:
            if ((left if left is not None else self.left)
                    >= (right if right is not None else self.right)):
                raise ValueError('left cannot be >= right')
            if ((bottom if bottom is not None else self.bottom)
                    >= (top if top is not None else self.top)):
                raise ValueError('bottom cannot be >= top')
        if left is not None:
            self.left = left
        if right is not None:
            self.right = right
        if bottom is not None:
            self.bottom = bottom
        if top is not None:
            self.top = top
        if wspace is not None:
            self.wspace = wspace
        if hspace is not None:
            self.hspace = hspace


class Figure(Artist):
    """
    The top level container for all the plot elements.

    The Figure instance supports callbacks through a *callbacks* attribute
    which is a `.CallbackRegistry` instance.  The events you can connect to
    are 'dpi_changed', and the callback will be called with ``func(fig)`` where
    fig is the `Figure` instance.

    Attributes
    ----------
    patch
        The `.Rectangle` instance representing the figure background patch.

    suppressComposite
        For multiple figure images, the figure will make composite images
        depending on the renderer option_image_nocomposite function.  If
        *suppressComposite* is a boolean, this will override the renderer.
    """

    def __str__(self):
        return "Figure(%gx%g)" % tuple(self.bbox.size)

    def __repr__(self):
        return "<{clsname} size {h:g}x{w:g} with {naxes} Axes>".format(
            clsname=self.__class__.__name__,
            h=self.bbox.size[0], w=self.bbox.size[1],
            naxes=len(self.axes),
        )

    def __init__(self,
                 figsize=None,
                 dpi=None,
                 facecolor=None,
                 edgecolor=None,
                 linewidth=0.0,
                 frameon=None,
                 subplotpars=None,  # rc figure.subplot.*
                 tight_layout=None,  # rc figure.autolayout
                 constrained_layout=None,  # rc figure.constrained_layout.use
                 ):
        """
        Parameters
        ----------
        figsize : 2-tuple of floats, default: :rc:`figure.figsize`
            Figure dimension ``(width, height)`` in inches.

        dpi : float, default: :rc:`figure.dpi`
            Dots per inch.

        facecolor : default: :rc:`figure.facecolor`
            The figure patch facecolor.

        edgecolor : default: :rc:`figure.edgecolor`
            The figure patch edge color.

        linewidth : float
            The linewidth of the frame (i.e. the edge linewidth of the figure
            patch).

        frameon : bool, default: :rc:`figure.frameon`
            If ``False``, suppress drawing the figure background patch.

        subplotpars : `SubplotParams`
            Subplot parameters. If not given, the default subplot
            parameters :rc:`figure.subplot.*` are used.

        tight_layout : bool or dict, default: :rc:`figure.autolayout`
            If ``False`` use *subplotpars*. If ``True`` adjust subplot
            parameters using `.tight_layout` with default padding.
            When providing a dict containing the keys ``pad``, ``w_pad``,
            ``h_pad``, and ``rect``, the default `.tight_layout` paddings
            will be overridden.

        constrained_layout : bool, default: :rc:`figure.constrained_layout.use`
            If ``True`` use constrained layout to adjust positioning of plot
            elements.  Like ``tight_layout``, but designed to be more
            flexible.  See
            :doc:`/tutorials/intermediate/constrainedlayout_guide`
            for examples.  (Note: does not work with `add_subplot` or
            `~.pyplot.subplot2grid`.)
        """
        super().__init__()
        # remove the non-figure artist _axes property
        # as it makes no sense for a figure to be _in_ an axes
        # this is used by the property methods in the artist base class
        # which are over-ridden in this class
        del self._axes
        self.callbacks = cbook.CallbackRegistry()

        if figsize is None:
            figsize = mpl.rcParams['figure.figsize']
        if dpi is None:
            dpi = mpl.rcParams['figure.dpi']
        if facecolor is None:
            facecolor = mpl.rcParams['figure.facecolor']
        if edgecolor is None:
            edgecolor = mpl.rcParams['figure.edgecolor']
        if frameon is None:
            frameon = mpl.rcParams['figure.frameon']

        if not np.isfinite(figsize).all() or (np.array(figsize) < 0).any():
            raise ValueError('figure size must be positive finite not '
                             f'{figsize}')
        self.bbox_inches = Bbox.from_bounds(0, 0, *figsize)

        self.dpi_scale_trans = Affine2D().scale(dpi)
        # do not use property as it will trigger
        self._dpi = dpi
        self.bbox = TransformedBbox(self.bbox_inches, self.dpi_scale_trans)

        self.transFigure = BboxTransformTo(self.bbox)

        self.patch = Rectangle(
            xy=(0, 0), width=1, height=1, visible=frameon,
            facecolor=facecolor, edgecolor=edgecolor, linewidth=linewidth,
            # Don't let the figure patch influence bbox calculation.
            in_layout=False)
        self._set_artist_props(self.patch)
        self.patch.set_antialiased(False)

        FigureCanvasBase(self)  # Set self.canvas.
        self._suptitle = None

        if subplotpars is None:
            subplotpars = SubplotParams()

        self.subplotpars = subplotpars
        # constrained_layout:
        self._layoutbox = None
        # set in set_constrained_layout_pads()
        self.set_constrained_layout(constrained_layout)

        self.set_tight_layout(tight_layout)

        self._axstack = _AxesStack()  # track all figure axes and current axes
        self.clf()
        self._cachedRenderer = None

        # groupers to keep track of x and y labels we want to align.
        # see self.align_xlabels and self.align_ylabels and
        # axis._get_tick_boxes_siblings
        self._align_xlabel_grp = cbook.Grouper()
        self._align_ylabel_grp = cbook.Grouper()

        # list of child gridspecs for this figure
        self._gridspecs = []

    # TODO: I'd like to dynamically add the _repr_html_ method
    # to the figure in the right context, but then IPython doesn't
    # use it, for some reason.

    def _repr_html_(self):
        # We can't use "isinstance" here, because then we'd end up importing
        # webagg unconditionally.
        if 'WebAgg' in type(self.canvas).__name__:
            from matplotlib.backends import backend_webagg
            return backend_webagg.ipython_inline_display(self)

    def show(self, warn=True):
        """
        If using a GUI backend with pyplot, display the figure window.

        If the figure was not created using `~.pyplot.figure`, it will lack
        a `~.backend_bases.FigureManagerBase`, and this method will raise an
        AttributeError.

        .. warning::

            This does not manage an GUI event loop. Consequently, the figure
            may only be shown briefly or not shown at all if you or your
            environment are not managing an event loop.

            Proper use cases for `.Figure.show` include running this from a
            GUI application or an IPython shell.

            If you're running a pure python shell or executing a non-GUI
            python script, you should use `matplotlib.pyplot.show` instead,
            which takes care of managing the event loop for you.

        Parameters
        ----------
        warn : bool, default: True
            If ``True`` and we are not running headless (i.e. on Linux with an
            unset DISPLAY), issue warning when called on a non-GUI backend.
        """
        if self.canvas.manager is None:
            raise AttributeError(
                "Figure.show works only for figures managed by pyplot, "
                "normally created by pyplot.figure()")
        try:
            self.canvas.manager.show()
        except NonGuiException as exc:
            cbook._warn_external(str(exc))

    def get_axes(self):
        """
        Return a list of axes in the Figure. You can access and modify the
        axes in the Figure through this list.

        Do not modify the list itself. Instead, use `~Figure.add_axes`,
        `~.Figure.add_subplot` or `~.Figure.delaxes` to add or remove an axes.

        Note: This is equivalent to the property `~.Figure.axes`.
        """
        return self._axstack.as_list()

    axes = property(get_axes, doc="""
        List of axes in the Figure.  You can access and modify the axes in the
        Figure through this list.

        Do not modify the list itself. Instead, use "`~Figure.add_axes`,
        `~.Figure.add_subplot` or `~.Figure.delaxes` to add or remove an axes.
        """)

    def _get_dpi(self):
        return self._dpi

    def _set_dpi(self, dpi, forward=True):
        """
        Parameters
        ----------
        dpi : float

        forward : bool
            Passed on to `~.Figure.set_size_inches`
        """
        if dpi == self._dpi:
            # We don't want to cause undue events in backends.
            return
        self._dpi = dpi
        self.dpi_scale_trans.clear().scale(dpi)
        w, h = self.get_size_inches()
        self.set_size_inches(w, h, forward=forward)
        self.callbacks.process('dpi_changed', self)

    dpi = property(_get_dpi, _set_dpi, doc="The resolution in dots per inch.")

    def get_tight_layout(self):
        """Return whether `.tight_layout` is called when drawing."""
        return self._tight

    def set_tight_layout(self, tight):
        """
        Set whether and how `.tight_layout` is called when drawing.

        Parameters
        ----------
        tight : bool or dict with keys "pad", "w_pad", "h_pad", "rect" or None
            If a bool, sets whether to call `.tight_layout` upon drawing.
            If ``None``, use the ``figure.autolayout`` rcparam instead.
            If a dict, pass it as kwargs to `.tight_layout`, overriding the
            default paddings.
        """
        if tight is None:
            tight = mpl.rcParams['figure.autolayout']
        self._tight = bool(tight)
        self._tight_parameters = tight if isinstance(tight, dict) else {}
        self.stale = True

    def get_constrained_layout(self):
        """
        Return whether constrained layout is being used.

        See :doc:`/tutorials/intermediate/constrainedlayout_guide`.
        """
        return self._constrained

    def set_constrained_layout(self, constrained):
        """
        Set whether ``constrained_layout`` is used upon drawing. If None,
        :rc:`figure.constrained_layout.use` value will be used.

        When providing a dict containing the keys `w_pad`, `h_pad`
        the default ``constrained_layout`` paddings will be
        overridden.  These pads are in inches and default to 3.0/72.0.
        ``w_pad`` is the width padding and ``h_pad`` is the height padding.

        See :doc:`/tutorials/intermediate/constrainedlayout_guide`.

        Parameters
        ----------
        constrained : bool or dict or None
        """
        self._constrained_layout_pads = dict()
        self._constrained_layout_pads['w_pad'] = None
        self._constrained_layout_pads['h_pad'] = None
        self._constrained_layout_pads['wspace'] = None
        self._constrained_layout_pads['hspace'] = None
        if constrained is None:
            constrained = mpl.rcParams['figure.constrained_layout.use']
        self._constrained = bool(constrained)
        if isinstance(constrained, dict):
            self.set_constrained_layout_pads(**constrained)
        else:
            self.set_constrained_layout_pads()

        self.stale = True

    def set_constrained_layout_pads(self, **kwargs):
        """
        Set padding for ``constrained_layout``.  Note the kwargs can be passed
        as a dictionary ``fig.set_constrained_layout(**paddict)``.

        See :doc:`/tutorials/intermediate/constrainedlayout_guide`.

        Parameters
        ----------
        w_pad : float
            Width padding in inches.  This is the pad around axes
            and is meant to make sure there is enough room for fonts to
            look good.  Defaults to 3 pts = 0.04167 inches

        h_pad : float
            Height padding in inches. Defaults to 3 pts.

        wspace : float
            Width padding between subplots, expressed as a fraction of the
            subplot width.  The total padding ends up being w_pad + wspace.

        hspace : float
            Height padding between subplots, expressed as a fraction of the
            subplot width. The total padding ends up being h_pad + hspace.

        """

        todo = ['w_pad', 'h_pad', 'wspace', 'hspace']
        for td in todo:
            if td in kwargs and kwargs[td] is not None:
                self._constrained_layout_pads[td] = kwargs[td]
            else:
                self._constrained_layout_pads[td] = (
                    mpl.rcParams['figure.constrained_layout.' + td])

    def get_constrained_layout_pads(self, relative=False):
        """
        Get padding for ``constrained_layout``.

        Returns a list of ``w_pad, h_pad`` in inches and
        ``wspace`` and ``hspace`` as fractions of the subplot.

        See :doc:`/tutorials/intermediate/constrainedlayout_guide`.

        Parameters
        ----------
        relative : bool
            If `True`, then convert from inches to figure relative.
        """
        w_pad = self._constrained_layout_pads['w_pad']
        h_pad = self._constrained_layout_pads['h_pad']
        wspace = self._constrained_layout_pads['wspace']
        hspace = self._constrained_layout_pads['hspace']

        if relative and (w_pad is not None or h_pad is not None):
            renderer0 = layoutbox.get_renderer(self)
            dpi = renderer0.dpi
            w_pad = w_pad * dpi / renderer0.width
            h_pad = h_pad * dpi / renderer0.height

        return w_pad, h_pad, wspace, hspace

    def autofmt_xdate(
            self, bottom=0.2, rotation=30, ha='right', which='major'):
        """
        Date ticklabels often overlap, so it is useful to rotate them
        and right align them.  Also, a common use case is a number of
        subplots with shared xaxes where the x-axis is date data.  The
        ticklabels are often long, and it helps to rotate them on the
        bottom subplot and turn them off on other subplots, as well as
        turn off xlabels.

        Parameters
        ----------
        bottom : float, default: 0.2
            The bottom of the subplots for `subplots_adjust`.
        rotation : float, default: 30 degrees
            The rotation angle of the xtick labels in degrees.
        ha : {'left', 'center', 'right'}, default: 'right'
            The horizontal alignment of the xticklabels.
        which : {'major', 'minor', 'both'}, default: 'major'
            Selects which ticklabels to rotate.
        """
        if which is None:
            cbook.warn_deprecated(
                "3.3", message="Support for passing which=None to mean "
                "which='major' is deprecated since %(since)s and will be "
                "removed %(removal)s.")
        allsubplots = all(hasattr(ax, 'is_last_row') for ax in self.axes)
        if len(self.axes) == 1:
            for label in self.axes[0].get_xticklabels(which=which):
                label.set_ha(ha)
                label.set_rotation(rotation)
        else:
            if allsubplots:
                for ax in self.get_axes():
                    if ax.is_last_row():
                        for label in ax.get_xticklabels(which=which):
                            label.set_ha(ha)
                            label.set_rotation(rotation)
                    else:
                        for label in ax.get_xticklabels(which=which):
                            label.set_visible(False)
                        ax.set_xlabel('')

        if allsubplots:
            self.subplots_adjust(bottom=bottom)
        self.stale = True

    def get_children(self):
        """Get a list of artists contained in the figure."""
        return [self.patch,
                *self.artists,
                *self.axes,
                *self.lines,
                *self.patches,
                *self.texts,
                *self.images,
                *self.legends]

    def contains(self, mouseevent):
        """
        Test whether the mouse event occurred on the figure.

        Returns
        -------
            bool, {}
        """
        inside, info = self._default_contains(mouseevent, figure=self)
        if inside is not None:
            return inside, info
        inside = self.bbox.contains(mouseevent.x, mouseevent.y)
        return inside, {}

    def get_window_extent(self, *args, **kwargs):
        """
        Return the figure bounding box in display space. Arguments are ignored.
        """
        return self.bbox

    def suptitle(self, t, **kwargs):
        """
        Add a centered title to the figure.

        Parameters
        ----------
        t : str
            The title text.

        x : float, default 0.5
            The x location of the text in figure coordinates.

        y : float, default 0.98
            The y location of the text in figure coordinates.

        horizontalalignment, ha : {'center', 'left', right'}, default: 'center'
            The horizontal alignment of the text relative to (*x*, *y*).

        verticalalignment, va : {'top', 'center', 'bottom', 'baseline'}, \
default: 'top'
            The vertical alignment of the text relative to (*x*, *y*).

        fontsize, size : default: :rc:`figure.titlesize`
            The font size of the text. See `.Text.set_size` for possible
            values.

        fontweight, weight : default: :rc:`figure.titleweight`
            The font weight of the text. See `.Text.set_weight` for possible
            values.

        Returns
        -------
        text
            The `.Text` instance of the title.

        Other Parameters
        ----------------
        fontproperties : None or dict, optional
            A dict of font properties. If *fontproperties* is given the
            default values for font size and weight are taken from the
            `.FontProperties` defaults. :rc:`figure.titlesize` and
            :rc:`figure.titleweight` are ignored in this case.

        **kwargs
            Additional kwargs are `matplotlib.text.Text` properties.

        Examples
        --------
        >>> fig.suptitle('This is the figure title', fontsize=12)
        """
        manual_position = ('x' in kwargs or 'y' in kwargs)

        x = kwargs.pop('x', 0.5)
        y = kwargs.pop('y', 0.98)

        if 'horizontalalignment' not in kwargs and 'ha' not in kwargs:
            kwargs['horizontalalignment'] = 'center'
        if 'verticalalignment' not in kwargs and 'va' not in kwargs:
            kwargs['verticalalignment'] = 'top'

        if 'fontproperties' not in kwargs:
            if 'fontsize' not in kwargs and 'size' not in kwargs:
                kwargs['size'] = mpl.rcParams['figure.titlesize']
            if 'fontweight' not in kwargs and 'weight' not in kwargs:
                kwargs['weight'] = mpl.rcParams['figure.titleweight']

        sup = self.text(x, y, t, **kwargs)
        if self._suptitle is not None:
            self._suptitle.set_text(t)
            self._suptitle.set_position((x, y))
            self._suptitle.update_from(sup)
            sup.remove()
        else:
            self._suptitle = sup
            self._suptitle._layoutbox = None
            if self._layoutbox is not None and not manual_position:
                w_pad, h_pad, wspace, hspace =  \
                        self.get_constrained_layout_pads(relative=True)
                figlb = self._layoutbox
                self._suptitle._layoutbox = layoutbox.LayoutBox(
                        parent=figlb, artist=self._suptitle,
                        name=figlb.name+'.suptitle')
                # stack the suptitle on top of all the children.
                # Some day this should be on top of all the children in the
                # gridspec only.
                for child in figlb.children:
                    if child is not self._suptitle._layoutbox:
                        layoutbox.vstack([self._suptitle._layoutbox,
                                          child],
                                         padding=h_pad*2., strength='required')
        self.stale = True
        return self._suptitle

    def set_canvas(self, canvas):
        """
        Set the canvas that contains the figure

        Parameters
        ----------
        canvas : FigureCanvas
        """
        self.canvas = canvas

    def figimage(self, X, xo=0, yo=0, alpha=None, norm=None, cmap=None,
                 vmin=None, vmax=None, origin=None, resize=False, **kwargs):
        """
        Add a non-resampled image to the figure.

        The image is attached to the lower or upper left corner depending on
        *origin*.

        Parameters
        ----------
        X
            The image data. This is an array of one of the following shapes:

            - MxN: luminance (grayscale) values
            - MxNx3: RGB values
            - MxNx4: RGBA values

        xo, yo : int
            The *x*/*y* image offset in pixels.

        alpha : None or float
            The alpha blending value.

        norm : `matplotlib.colors.Normalize`
            A `.Normalize` instance to map the luminance to the
            interval [0, 1].

        cmap : str or `matplotlib.colors.Colormap`, default: :rc:`image.cmap`
            The colormap to use.

        vmin, vmax : float
            If *norm* is not given, these values set the data limits for the
            colormap.

        origin : {'upper', 'lower'}, default: :rc:`image.origin`
            Indicates where the [0, 0] index of the array is in the upper left
            or lower left corner of the axes.

        resize : bool
            If *True*, resize the figure to match the given image size.

        Returns
        -------
        `matplotlib.image.FigureImage`

        Other Parameters
        ----------------
        **kwargs
            Additional kwargs are `.Artist` kwargs passed on to `.FigureImage`.

        Notes
        -----
        figimage complements the axes image (`~matplotlib.axes.Axes.imshow`)
        which will be resampled to fit the current axes.  If you want
        a resampled image to fill the entire figure, you can define an
        `~matplotlib.axes.Axes` with extent [0, 0, 1, 1].

        Examples
        --------
        ::

            f = plt.figure()
            nx = int(f.get_figwidth() * f.dpi)
            ny = int(f.get_figheight() * f.dpi)
            data = np.random.random((ny, nx))
            f.figimage(data)
            plt.show()
        """
        if resize:
            dpi = self.get_dpi()
            figsize = [x / dpi for x in (X.shape[1], X.shape[0])]
            self.set_size_inches(figsize, forward=True)

        im = mimage.FigureImage(self, cmap, norm, xo, yo, origin, **kwargs)
        im.stale_callback = _stale_figure_callback

        im.set_array(X)
        im.set_alpha(alpha)
        if norm is None:
            im.set_clim(vmin, vmax)
        self.images.append(im)
        im._remove_method = self.images.remove
        self.stale = True
        return im

    def set_size_inches(self, w, h=None, forward=True):
        """
        Set the figure size in inches.

        Call signatures::

             fig.set_size_inches(w, h)  # OR
             fig.set_size_inches((w, h))

        Parameters
        ----------
        w : (float, float) or float
            Width and height in inches (if height not specified as a separate
            argument) or width.
        h : float
            Height in inches.
        forward : bool, default: True
            If ``True``, the canvas size is automatically updated, e.g.,
            you can resize the figure window from the shell.

        See Also
        --------
        matplotlib.figure.Figure.get_size_inches
        matplotlib.figure.Figure.set_figwidth
        matplotlib.figure.Figure.set_figheight

        Notes
        -----
        To transform from pixels to inches divide by `Figure.dpi`.
        """
        if h is None:  # Got called with a single pair as argument.
            w, h = w
        size = np.array([w, h])
        if not np.isfinite(size).all() or (size < 0).any():
            raise ValueError(f'figure size must be positive finite not {size}')
        self.bbox_inches.p1 = size
        if forward:
            canvas = getattr(self, 'canvas')
            if canvas is not None:
                dpi_ratio = getattr(canvas, '_dpi_ratio', 1)
                manager = getattr(canvas, 'manager', None)
                if manager is not None:
                    manager.resize(*(size * self.dpi / dpi_ratio).astype(int))
        self.stale = True

    def get_size_inches(self):
        """
        Return the current size of the figure in inches.

        Returns
        -------
        ndarray
           The size (width, height) of the figure in inches.

        See Also
        --------
        matplotlib.figure.Figure.set_size_inches
        matplotlib.figure.Figure.get_figwidth
        matplotlib.figure.Figure.get_figheight

        Notes
        -----
        The size in pixels can be obtained by multiplying with `Figure.dpi`.
        """
        return np.array(self.bbox_inches.p1)

    def get_edgecolor(self):
        """Get the edge color of the Figure rectangle."""
        return self.patch.get_edgecolor()

    def get_facecolor(self):
        """Get the face color of the Figure rectangle."""
        return self.patch.get_facecolor()

    def get_figwidth(self):
        """Return the figure width in inches."""
        return self.bbox_inches.width

    def get_figheight(self):
        """Return the figure height in inches."""
        return self.bbox_inches.height

    def get_dpi(self):
        """Return the resolution in dots per inch as a float."""
        return self.dpi

    def get_frameon(self):
        """
        Return the figure's background patch visibility, i.e.
        whether the figure background will be drawn. Equivalent to
        ``Figure.patch.get_visible()``.
        """
        return self.patch.get_visible()

    def set_edgecolor(self, color):
        """
        Set the edge color of the Figure rectangle.

        Parameters
        ----------
        color : color
        """
        self.patch.set_edgecolor(color)

    def set_facecolor(self, color):
        """
        Set the face color of the Figure rectangle.

        Parameters
        ----------
        color : color
        """
        self.patch.set_facecolor(color)

    def set_dpi(self, val):
        """
        Set the resolution of the figure in dots-per-inch.

        Parameters
        ----------
        val : float
        """
        self.dpi = val
        self.stale = True

    def set_figwidth(self, val, forward=True):
        """
        Set the width of the figure in inches.

        Parameters
        ----------
        val : float
        forward : bool
            See `set_size_inches`.

        See Also
        --------
        matplotlib.figure.Figure.set_figheight
        matplotlib.figure.Figure.set_size_inches
        """
        self.set_size_inches(val, self.get_figheight(), forward=forward)

    def set_figheight(self, val, forward=True):
        """
        Set the height of the figure in inches.

        Parameters
        ----------
        val : float
        forward : bool
            See `set_size_inches`.

        See Also
        --------
        matplotlib.figure.Figure.set_figwidth
        matplotlib.figure.Figure.set_size_inches
        """
        self.set_size_inches(self.get_figwidth(), val, forward=forward)

    def set_frameon(self, b):
        """
        Set the figure's background patch visibility, i.e.
        whether the figure background will be drawn. Equivalent to
        ``Figure.patch.set_visible()``.

        Parameters
        ----------
        b : bool
        """
        self.patch.set_visible(b)
        self.stale = True

    frameon = property(get_frameon, set_frameon)

    def add_artist(self, artist, clip=False):
        """
        Add an `.Artist` to the figure.

        Usually artists are added to axes objects using `.Axes.add_artist`;
        this method can be used in the rare cases where one needs to add
        artists directly to the figure instead.

        Parameters
        ----------
        artist : `~matplotlib.artist.Artist`
            The artist to add to the figure. If the added artist has no
            transform previously set, its transform will be set to
            ``figure.transFigure``.
        clip : bool, default: False
            Whether the added artist should be clipped by the figure patch.

        Returns
        -------
        `~matplotlib.artist.Artist`
            The added artist.
        """
        artist.set_figure(self)
        self.artists.append(artist)
        artist._remove_method = self.artists.remove

        if not artist.is_transform_set():
            artist.set_transform(self.transFigure)

        if clip:
            artist.set_clip_path(self.patch)

        self.stale = True
        return artist

    def _make_key(self, *args, **kwargs):
        """Make a hashable key out of args and kwargs."""

        def fixitems(items):
            # items may have arrays and lists in them, so convert them
            # to tuples for the key
            ret = []
            for k, v in items:
                # some objects can define __getitem__ without being
                # iterable and in those cases the conversion to tuples
                # will fail. So instead of using the np.iterable(v) function
                # we simply try and convert to a tuple, and proceed if not.
                try:
                    v = tuple(v)
                except Exception:
                    pass
                ret.append((k, v))
            return tuple(ret)

        def fixlist(args):
            ret = []
            for a in args:
                if np.iterable(a):
                    a = tuple(a)
                ret.append(a)
            return tuple(ret)

        key = fixlist(args), fixitems(kwargs.items())
        return key

    def _process_projection_requirements(
            self, *args, polar=False, projection=None, **kwargs):
        """
        Handle the args/kwargs to add_axes/add_subplot/gca, returning::

            (axes_proj_class, proj_class_kwargs, proj_stack_key)

        which can be used for new axes initialization/identification.
        """
        if polar:
            if projection is not None and projection != 'polar':
                raise ValueError(
                    "polar=True, yet projection=%r. "
                    "Only one of these arguments should be supplied." %
                    projection)
            projection = 'polar'

        if isinstance(projection, str) or projection is None:
            projection_class = projections.get_projection_class(projection)
        elif hasattr(projection, '_as_mpl_axes'):
            projection_class, extra_kwargs = projection._as_mpl_axes()
            kwargs.update(**extra_kwargs)
        else:
            raise TypeError('projection must be a string, None or implement a '
                            '_as_mpl_axes method. Got %r' % projection)

        # Make the key without projection kwargs, this is used as a unique
        # lookup for axes instances
        key = self._make_key(*args, **kwargs)

        return projection_class, kwargs, key

    @docstring.dedent_interpd
    def add_axes(self, *args, **kwargs):
        """
        Add an axes to the figure.

        Call signatures::

            add_axes(rect, projection=None, polar=False, **kwargs)
            add_axes(ax)

        Parameters
        ----------
        rect : sequence of float
            The dimensions [left, bottom, width, height] of the new axes. All
            quantities are in fractions of figure width and height.

        projection : {None, 'aitoff', 'hammer', 'lambert', 'mollweide', \
'polar', 'rectilinear', str}, optional
            The projection type of the `~.axes.Axes`. *str* is the name of
            a custom projection, see `~matplotlib.projections`. The default
            None results in a 'rectilinear' projection.

        polar : bool, default: False
            If True, equivalent to projection='polar'.

        sharex, sharey : `~.axes.Axes`, optional
            Share the x or y `~matplotlib.axis` with sharex and/or sharey.
            The axis will have the same limits, ticks, and scale as the axis
            of the shared axes.

        label : str
            A label for the returned axes.

        Returns
        -------
        `~.axes.Axes`, or a subclass of `~.axes.Axes`
            The returned axes class depends on the projection used. It is
            `~.axes.Axes` if rectilinear projection is used and
            `.projections.polar.PolarAxes` if polar projection is used.

        Other Parameters
        ----------------
        **kwargs
            This method also takes the keyword arguments for
            the returned axes class. The keyword arguments for the
            rectilinear axes class `~.axes.Axes` can be found in
            the following table but there might also be other keyword
            arguments if another projection is used, see the actual axes
            class.

            %(Axes)s

        Notes
        -----
        If the figure already has an axes with key (*args*,
        *kwargs*) then it will simply make that axes current and
        return it.  This behavior is deprecated. Meanwhile, if you do
        not want this behavior (i.e., you want to force the creation of a
        new axes), you must use a unique set of args and kwargs.  The axes
        *label* attribute has been exposed for this purpose: if you want
        two axes that are otherwise identical to be added to the figure,
        make sure you give them unique labels.

        In rare circumstances, `.add_axes` may be called with a single
        argument, a axes instance already created in the present figure but
        not in the figure's list of axes.

        See Also
        --------
        .Figure.add_subplot
        .pyplot.subplot
        .pyplot.axes
        .Figure.subplots
        .pyplot.subplots

        Examples
        --------
        Some simple examples::

            rect = l, b, w, h
            fig = plt.figure()
            fig.add_axes(rect, label=label1)
            fig.add_axes(rect, label=label2)
            fig.add_axes(rect, frameon=False, facecolor='g')
            fig.add_axes(rect, polar=True)
            ax = fig.add_axes(rect, projection='polar')
            fig.delaxes(ax)
            fig.add_axes(ax)
        """

        if not len(args) and 'rect' not in kwargs:
            cbook.warn_deprecated(
                "3.3",
                message="Calling add_axes() without argument is "
                "deprecated since %(since)s and will be removed %(removal)s. "
                "You may want to use add_subplot() instead.")
            return
        elif 'rect' in kwargs:
            if len(args):
                raise TypeError(
                    "add_axes() got multiple values for argument 'rect'")
            args = (kwargs.pop('rect'), )

        # shortcut the projection "key" modifications later on, if an axes
        # with the exact args/kwargs exists, return it immediately.
        key = self._make_key(*args, **kwargs)
        ax = self._axstack.get(key)
        if ax is not None:
            self.sca(ax)
            return ax

        if isinstance(args[0], Axes):
            a = args[0]
            if a.get_figure() is not self:
                raise ValueError(
                    "The Axes must have been created in the present figure")
        else:
            rect = args[0]
            if not np.isfinite(rect).all():
                raise ValueError('all entries in rect must be finite '
                                 'not {}'.format(rect))
            projection_class, kwargs, key = \
                self._process_projection_requirements(*args, **kwargs)

            # check that an axes of this type doesn't already exist, if it
            # does, set it as active and return it
            ax = self._axstack.get(key)
            if isinstance(ax, projection_class):
                self.sca(ax)
                return ax

            # create the new axes using the axes class given
            a = projection_class(self, rect, **kwargs)

        return self._add_axes_internal(key, a)

    @docstring.dedent_interpd
    def add_subplot(self, *args, **kwargs):
        """
        Add an `~.axes.Axes` to the figure as part of a subplot arrangement.

        Call signatures::

           add_subplot(nrows, ncols, index, **kwargs)
           add_subplot(pos, **kwargs)
           add_subplot(ax)
           add_subplot()

        Parameters
        ----------
        *args : int, (int, int, *index*), or `.SubplotSpec`, default: (1, 1, 1)
            The position of the subplot described by one of

            - Three integers (*nrows*, *ncols*, *index*). The subplot will
              take the *index* position on a grid with *nrows* rows and
              *ncols* columns. *index* starts at 1 in the upper left corner
              and increases to the right.  *index* can also be a two-tuple
              specifying the (*first*, *last*) indices (1-based, and including
              *last*) of the subplot, e.g., ``fig.add_subplot(3, 1, (1, 2))``
              makes a subplot that spans the upper 2/3 of the figure.
            - A 3-digit integer. The digits are interpreted as if given
              separately as three single-digit integers, i.e.
              ``fig.add_subplot(235)`` is the same as
              ``fig.add_subplot(2, 3, 5)``. Note that this can only be used
              if there are no more than 9 subplots.
            - A `.SubplotSpec`.

            In rare circumstances, `.add_subplot` may be called with a single
            argument, a subplot axes instance already created in the
            present figure but not in the figure's list of axes.

        projection : {None, 'aitoff', 'hammer', 'lambert', 'mollweide', \
'polar', 'rectilinear', str}, optional
            The projection type of the subplot (`~.axes.Axes`). *str* is the
            name of a custom projection, see `~matplotlib.projections`. The
            default None results in a 'rectilinear' projection.

        polar : bool, default: False
            If True, equivalent to projection='polar'.

        sharex, sharey : `~.axes.Axes`, optional
            Share the x or y `~matplotlib.axis` with sharex and/or sharey.
            The axis will have the same limits, ticks, and scale as the axis
            of the shared axes.

        label : str
            A label for the returned axes.

        Returns
        -------
        `.axes.SubplotBase`, or another subclass of `~.axes.Axes`

            The axes of the subplot. The returned axes base class depends on
            the projection used. It is `~.axes.Axes` if rectilinear projection
            is used and `.projections.polar.PolarAxes` if polar projection
            is used. The returned axes is then a subplot subclass of the
            base class.

        Other Parameters
        ----------------
        **kwargs
            This method also takes the keyword arguments for the returned axes
            base class; except for the *figure* argument. The keyword arguments
            for the rectilinear base class `~.axes.Axes` can be found in
            the following table but there might also be other keyword
            arguments if another projection is used.

            %(Axes)s

        Notes
        -----
        If the figure already has a subplot with key (*args*,
        *kwargs*) then it will simply make that subplot current and
        return it.  This behavior is deprecated. Meanwhile, if you do
        not want this behavior (i.e., you want to force the creation of a
        new subplot), you must use a unique set of args and kwargs.  The axes
        *label* attribute has been exposed for this purpose: if you want
        two subplots that are otherwise identical to be added to the figure,
        make sure you give them unique labels.

        See Also
        --------
        .Figure.add_axes
        .pyplot.subplot
        .pyplot.axes
        .Figure.subplots
        .pyplot.subplots

        Examples
        --------
        ::

            fig = plt.figure()

            fig.add_subplot(231)
            ax1 = fig.add_subplot(2, 3, 1)  # equivalent but more general

            fig.add_subplot(232, frameon=False)  # subplot with no frame
            fig.add_subplot(233, projection='polar')  # polar subplot
            fig.add_subplot(234, sharex=ax1)  # subplot sharing x-axis with ax1
            fig.add_subplot(235, facecolor="red")  # red subplot

            ax1.remove()  # delete ax1 from the figure
            fig.add_subplot(ax1)  # add ax1 back to the figure
        """
        if 'figure' in kwargs:
            # Axes itself allows for a 'figure' kwarg, but since we want to
            # bind the created Axes to self, it is not allowed here.
            raise TypeError(
                "add_subplot() got an unexpected keyword argument 'figure'")

        if len(args) == 1 and isinstance(args[0], SubplotBase):
            ax = args[0]
            if ax.get_figure() is not self:
                raise ValueError("The Subplot must have been created in "
                                 "the present figure")
            # make a key for the subplot (which includes the axes object id
            # in the hash)
            key = self._make_key(*args, **kwargs)

        else:
            if not args:
                args = (1, 1, 1)
            # Normalize correct ijk values to (i, j, k) here so that
            # add_subplot(111) == add_subplot(1, 1, 1).  Invalid values will
            # trigger errors later (via SubplotSpec._from_subplot_args).
            if (len(args) == 1 and isinstance(args[0], Integral)
                    and 100 <= args[0] <= 999):
                args = tuple(map(int, str(args[0])))
            projection_class, kwargs, key = \
                self._process_projection_requirements(*args, **kwargs)
            ax = self._axstack.get(key)  # search axes with this key in stack
            if ax is not None:
                if isinstance(ax, projection_class):
                    # the axes already existed, so set it as active & return
                    self.sca(ax)
                    return ax
                else:
                    # Undocumented convenience behavior:
                    # subplot(111); subplot(111, projection='polar')
                    # will replace the first with the second.
                    # Without this, add_subplot would be simpler and
                    # more similar to add_axes.
                    self._axstack.remove(ax)
            ax = subplot_class_factory(projection_class)(self, *args, **kwargs)

        return self._add_axes_internal(key, ax)

    def _add_axes_internal(self, key, ax):
        """Private helper for `add_axes` and `add_subplot`."""
        self._axstack.add(key, ax)
        self.sca(ax)
        ax._remove_method = self.delaxes
        self.stale = True
        ax.stale_callback = _stale_figure_callback
        return ax

    @cbook._make_keyword_only("3.3", "sharex")
    def subplots(self, nrows=1, ncols=1, sharex=False, sharey=False,
                 squeeze=True, subplot_kw=None, gridspec_kw=None):
        """
        Add a set of subplots to this figure.

        This utility wrapper makes it convenient to create common layouts of
        subplots in a single call.

        Parameters
        ----------
        nrows, ncols : int, default: 1
            Number of rows/columns of the subplot grid.

        sharex, sharey : bool or {'none', 'all', 'row', 'col'}, default: False
            Controls sharing of properties among x (*sharex*) or y (*sharey*)
            axes:

            - True or 'all': x- or y-axis will be shared among all subplots.
            - False or 'none': each subplot x- or y-axis will be independent.
            - 'row': each subplot row will share an x- or y-axis.
            - 'col': each subplot column will share an x- or y-axis.

            When subplots have a shared x-axis along a column, only the x tick
            labels of the bottom subplot are created. Similarly, when subplots
            have a shared y-axis along a row, only the y tick labels of the
            first column subplot are created. To later turn other subplots'
            ticklabels on, use `~matplotlib.axes.Axes.tick_params`.

        squeeze : bool, default: True
            - If True, extra dimensions are squeezed out from the returned
              array of Axes:

              - if only one subplot is constructed (nrows=ncols=1), the
                resulting single Axes object is returned as a scalar.
              - for Nx1 or 1xM subplots, the returned object is a 1D numpy
                object array of Axes objects.
              - for NxM, subplots with N>1 and M>1 are returned as a 2D array.

            - If False, no squeezing at all is done: the returned Axes object
              is always a 2D array containing Axes instances, even if it ends
              up being 1x1.

        subplot_kw : dict, optional
            Dict with keywords passed to the `.Figure.add_subplot` call used to
            create each subplot.

        gridspec_kw : dict, optional
            Dict with keywords passed to the
            `~matplotlib.gridspec.GridSpec` constructor used to create
            the grid the subplots are placed on.

        Returns
        -------
        `~.axes.Axes` or array of Axes
            Either a single `~matplotlib.axes.Axes` object or an array of Axes
            objects if more than one subplot was created. The dimensions of the
            resulting array can be controlled with the *squeeze* keyword, see
            above.

        See Also
        --------
        .pyplot.subplots
        .Figure.add_subplot
        .pyplot.subplot

        Examples
        --------
        ::

            # First create some toy data:
            x = np.linspace(0, 2*np.pi, 400)
            y = np.sin(x**2)

            # Create a figure
            plt.figure()

            # Create a subplot
            ax = fig.subplots()
            ax.plot(x, y)
            ax.set_title('Simple plot')

            # Create two subplots and unpack the output array immediately
            ax1, ax2 = fig.subplots(1, 2, sharey=True)
            ax1.plot(x, y)
            ax1.set_title('Sharing Y axis')
            ax2.scatter(x, y)

            # Create four polar axes and access them through the returned array
            axes = fig.subplots(2, 2, subplot_kw=dict(polar=True))
            axes[0, 0].plot(x, y)
            axes[1, 1].scatter(x, y)

            # Share a X axis with each column of subplots
            fig.subplots(2, 2, sharex='col')

            # Share a Y axis with each row of subplots
            fig.subplots(2, 2, sharey='row')

            # Share both X and Y axes with all subplots
            fig.subplots(2, 2, sharex='all', sharey='all')

            # Note that this is the same as
            fig.subplots(2, 2, sharex=True, sharey=True)
        """
        if gridspec_kw is None:
            gridspec_kw = {}
        return (self.add_gridspec(nrows, ncols, figure=self, **gridspec_kw)
                .subplots(sharex=sharex, sharey=sharey, squeeze=squeeze,
                          subplot_kw=subplot_kw))

    @staticmethod
    def _normalize_grid_string(layout):
        layout = inspect.cleandoc(layout)
        return [list(ln) for ln in layout.strip('\n').split('\n')]

    def subplot_mosaic(self, layout, *, subplot_kw=None, gridspec_kw=None,
                       empty_sentinel='.'):
        """
        Build a layout of Axes based on ASCII art or nested lists.

        This is a helper function to build complex GridSpec layouts visually.

        .. note ::

           This API is provisional and may be revised in the future based on
           early user feedback.


        Parameters
        ----------
        layout : list of list of {hashable or nested} or str

            A visual layout of how you want your Axes to be arranged
            labeled as strings.  For example ::

               x = [['A panel', 'A panel', 'edge'],
                    ['C panel', '.',       'edge']]

            Produces 4 axes:

            - 'A panel' which is 1 row high and spans the first two columns
            - 'edge' which is 2 rows high and is on the right edge
            - 'C panel' which in 1 row and 1 column wide in the bottom left
            - a blank space 1 row and 1 column wide in the bottom center

            Any of the entries in the layout can be a list of lists
            of the same form to create nested layouts.

            If input is a str, then it must be of the form ::

              '''
              AAE
              C.E
              '''

            where each character is a column and each line is a row.
            This only allows only single character Axes labels and does
            not allow nesting but is very terse.

        subplot_kw : dict, optional
            Dictionary with keywords passed to the `.Figure.add_subplot` call
            used to create each subplot.

        gridspec_kw : dict, optional
            Dictionary with keywords passed to the `.GridSpec` constructor used
            to create the grid the subplots are placed on.

        empty_sentinel : object, optional
            Entry in the layout to mean "leave this space empty".  Defaults
            to ``'.'``. Note, if *layout* is a string, it is processed via
            `inspect.cleandoc` to remove leading white space, which may
            interfere with using white-space as the empty sentinel.

        Returns
        -------
        dict[label, Axes]
           A dictionary mapping the labels to the Axes objects.

        """
        subplot_kw = subplot_kw or {}
        gridspec_kw = gridspec_kw or {}
        # special-case string input
        if isinstance(layout, str):
            layout = self._normalize_grid_string(layout)

        def _make_array(inp):
            """
            Convert input into 2D array

            We need to have this internal function rather than
            ``np.asarray(..., dtype=object)`` so that a list of lists
            of lists does not get converted to an array of dimension >
            2

            Returns
            -------
            2D object array

            """
            r0, *rest = inp
            for j, r in enumerate(rest, start=1):
                if len(r0) != len(r):
                    raise ValueError(
                        "All of the rows must be the same length, however "
                        f"the first row ({r0!r}) has length {len(r0)} "
                        f"and row {j} ({r!r}) has length {len(r)}."
                    )
            out = np.zeros((len(inp), len(r0)), dtype=object)
            for j, r in enumerate(inp):
                for k, v in enumerate(r):
                    out[j, k] = v
            return out

        def _identify_keys_and_nested(layout):
            """
            Given a 2D object array, identify unique IDs and nested layouts

            Parameters
            ----------
            layout : 2D numpy object array

            Returns
            -------
            unique_ids : Set[object]
                The unique non-sub layout entries in this layout
            nested : Dict[Tuple[int, int]], 2D object array
            """
            unique_ids = set()
            nested = {}
            for j, row in enumerate(layout):
                for k, v in enumerate(row):
                    if v == empty_sentinel:
                        continue
                    elif not cbook.is_scalar_or_string(v):
                        nested[(j, k)] = _make_array(v)
                    else:
                        unique_ids.add(v)

            return unique_ids, nested

        def _do_layout(gs, layout, unique_ids, nested):
            """
            Recursively do the layout.

            Parameters
            ----------
            gs : GridSpec

            layout : 2D object array
                The input converted to a 2D numpy array for this level.

            unique_ids : Set[object]
                The identified scalar labels at this level of nesting.

            nested : Dict[Tuple[int, int]], 2D object array
                The identified nested layouts if any.

            Returns
            -------
            Dict[label, Axes]
                A flat dict of all of the Axes created.
            """
            rows, cols = layout.shape
            output = dict()

            # create the Axes at this level of nesting
            for name in unique_ids:
                indx = np.argwhere(layout == name)
                start_row, start_col = np.min(indx, axis=0)
                end_row, end_col = np.max(indx, axis=0) + 1
                slc = (slice(start_row, end_row), slice(start_col, end_col))

                if (layout[slc] != name).any():
                    raise ValueError(
                        f"While trying to layout\n{layout!r}\n"
                        f"we found that the label {name!r} specifies a "
                        "non-rectangular or non-contiguous area.")

                ax = self.add_subplot(
                    gs[slc], **{'label': str(name), **subplot_kw}
                )
                output[name] = ax

            # do any sub-layouts
            for (j, k), nested_layout in nested.items():
                rows, cols = nested_layout.shape
                nested_output = _do_layout(
                    gs[j, k].subgridspec(rows, cols, **gridspec_kw),
                    nested_layout,
                    *_identify_keys_and_nested(nested_layout)
                )
                overlap = set(output) & set(nested_output)
                if overlap:
                    raise ValueError(f"There are duplicate keys {overlap} "
                                     f"between the outer layout\n{layout!r}\n"
                                     f"and the nested layout\n{nested_layout}")
                output.update(nested_output)
            return output

        layout = _make_array(layout)
        rows, cols = layout.shape
        gs = self.add_gridspec(rows, cols, **gridspec_kw)
        ret = _do_layout(gs, layout, *_identify_keys_and_nested(layout))
        for k, ax in ret.items():
            if isinstance(k, str):
                ax.set_label(k)
        return ret

    def delaxes(self, ax):
        """
        Remove the `~.axes.Axes` *ax* from the figure; update the current axes.
        """

        def _reset_locators_and_formatters(axis):
            # Set the formatters and locators to be associated with axis
            # (where previously they may have been associated with another
            # Axis instance)
            #
            # Because set_major_formatter() etc. force isDefault_* to be False,
            # we have to manually check if the original formatter was a
            # default and manually set isDefault_* if that was the case.
            majfmt = axis.get_major_formatter()
            isDefault = majfmt.axis.isDefault_majfmt
            axis.set_major_formatter(majfmt)
            if isDefault:
                majfmt.axis.isDefault_majfmt = True

            majloc = axis.get_major_locator()
            isDefault = majloc.axis.isDefault_majloc
            axis.set_major_locator(majloc)
            if isDefault:
                majloc.axis.isDefault_majloc = True

            minfmt = axis.get_minor_formatter()
            isDefault = majloc.axis.isDefault_minfmt
            axis.set_minor_formatter(minfmt)
            if isDefault:
                minfmt.axis.isDefault_minfmt = True

            minloc = axis.get_minor_locator()
            isDefault = majloc.axis.isDefault_minloc
            axis.set_minor_locator(minloc)
            if isDefault:
                minloc.axis.isDefault_minloc = True

        def _break_share_link(ax, grouper):
            siblings = grouper.get_siblings(ax)
            if len(siblings) > 1:
                grouper.remove(ax)
                for last_ax in siblings:
                    if ax is not last_ax:
                        return last_ax
            return None

        self._axstack.remove(ax)
        self._axobservers.process("_axes_change_event", self)
        self.stale = True

        last_ax = _break_share_link(ax, ax._shared_y_axes)
        if last_ax is not None:
            _reset_locators_and_formatters(last_ax.yaxis)

        last_ax = _break_share_link(ax, ax._shared_x_axes)
        if last_ax is not None:
            _reset_locators_and_formatters(last_ax.xaxis)

    def clf(self, keep_observers=False):
        """
        Clear the figure.

        Set *keep_observers* to True if, for example,
        a gui widget is tracking the axes in the figure.
        """
        self.suppressComposite = None
        self.callbacks = cbook.CallbackRegistry()

        for ax in tuple(self.axes):  # Iterate over the copy.
            ax.cla()
            self.delaxes(ax)         # removes ax from self._axstack

        toolbar = getattr(self.canvas, 'toolbar', None)
        if toolbar is not None:
            toolbar.update()
        self._axstack.clear()
        self.artists = []
        self.lines = []
        self.patches = []
        self.texts = []
        self.images = []
        self.legends = []
        if not keep_observers:
            self._axobservers = cbook.CallbackRegistry()
        self._suptitle = None
        if self.get_constrained_layout():
            layoutbox.nonetree(self._layoutbox)
        self.stale = True

    def clear(self, keep_observers=False):
        """Clear the figure -- synonym for `clf`."""
        self.clf(keep_observers=keep_observers)

    @allow_rasterization
    def draw(self, renderer):
        # docstring inherited
        self._cachedRenderer = renderer

        # draw the figure bounding box, perhaps none for white figure
        if not self.get_visible():
            return

        artists = self.get_children()
        artists.remove(self.patch)
        artists = sorted(
            (artist for artist in artists if not artist.get_animated()),
            key=lambda artist: artist.get_zorder())

        for ax in self.axes:
            locator = ax.get_axes_locator()
            if locator:
                pos = locator(ax, renderer)
                ax.apply_aspect(pos)
            else:
                ax.apply_aspect()

            for child in ax.get_children():
                if hasattr(child, 'apply_aspect'):
                    locator = child.get_axes_locator()
                    if locator:
                        pos = locator(child, renderer)
                        child.apply_aspect(pos)
                    else:
                        child.apply_aspect()

        try:
            renderer.open_group('figure', gid=self.get_gid())
            if self.get_constrained_layout() and self.axes:
                self.execute_constrained_layout(renderer)
            if self.get_tight_layout() and self.axes:
                try:
                    self.tight_layout(**self._tight_parameters)
                except ValueError:
                    pass
                    # ValueError can occur when resizing a window.

            self.patch.draw(renderer)
            mimage._draw_list_compositing_images(
                renderer, self, artists, self.suppressComposite)

            renderer.close_group('figure')
        finally:
            self.stale = False

        self.canvas.draw_event(renderer)

    def draw_artist(self, a):
        """
        Draw `.Artist` instance *a* only.

        This can only be called after the figure has been drawn.
        """
        if self._cachedRenderer is None:
            raise AttributeError("draw_artist can only be used after an "
                                 "initial draw which caches the renderer")
        a.draw(self._cachedRenderer)

    # Note: in the docstring below, the newlines in the examples after the
    # calls to legend() allow replacing it with figlegend() to generate the
    # docstring of pyplot.figlegend.

    @docstring.dedent_interpd
    def legend(self, *args, **kwargs):
        """
        Place a legend on the figure.

        To make a legend from existing artists on every axes::

          legend()

        To make a legend for a list of lines and labels::

          legend(
              (line1, line2, line3),
              ('label1', 'label2', 'label3'),
              loc='upper right')

        These can also be specified by keyword::

          legend(
              handles=(line1, line2, line3),
              labels=('label1', 'label2', 'label3'),
              loc='upper right')

        Parameters
        ----------
        handles : list of `.Artist`, optional
            A list of Artists (lines, patches) to be added to the legend.
            Use this together with *labels*, if you need full control on what
            is shown in the legend and the automatic mechanism described above
            is not sufficient.

            The length of handles and labels should be the same in this
            case. If they are not, they are truncated to the smaller length.

        labels : list of str, optional
            A list of labels to show next to the artists.
            Use this together with *handles*, if you need full control on what
            is shown in the legend and the automatic mechanism described above
            is not sufficient.

        Returns
        -------
        `~matplotlib.legend.Legend`

        Other Parameters
        ----------------
        %(_legend_kw_doc)s

        Notes
        -----
        Some artists are not supported by this function.  See
        :doc:`/tutorials/intermediate/legend_guide` for details.
        """

        handles, labels, extra_args, kwargs = mlegend._parse_legend_args(
                self.axes,
                *args,
                **kwargs)
        # check for third arg
        if len(extra_args):
            # cbook.warn_deprecated(
            #     "2.1",
            #     message="Figure.legend will accept no more than two "
            #     "positional arguments in the future.  Use "
            #     "'fig.legend(handles, labels, loc=location)' "
            #     "instead.")
            # kwargs['loc'] = extra_args[0]
            # extra_args = extra_args[1:]
            pass
        transform = kwargs.pop('bbox_transform', self.transFigure)
        # explicitly set the bbox transform if the user hasn't.
        l = mlegend.Legend(self, handles, labels, *extra_args,
                           bbox_transform=transform, **kwargs)
        self.legends.append(l)
        l._remove_method = self.legends.remove
        self.stale = True
        return l

    @docstring.dedent_interpd
    def text(self, x, y, s, fontdict=None, **kwargs):
        """
        Add text to figure.

        Parameters
        ----------
        x, y : float
            The position to place the text. By default, this is in figure
            coordinates, floats in [0, 1]. The coordinate system can be changed
            using the *transform* keyword.

        s : str
            The text string.

        fontdict : dict, optional
            A dictionary to override the default text properties. If not given,
            the defaults are determined by :rc:`font.*`. Properties passed as
            *kwargs* override the corresponding ones given in *fontdict*.

        Returns
        -------
        `~.text.Text`

        Other Parameters
        ----------------
        **kwargs : `~matplotlib.text.Text` properties
            Other miscellaneous text parameters.

            %(Text)s

        See Also
        --------
        .Axes.text
        .pyplot.text
        """
        effective_kwargs = {
            'transform': self.transFigure,
            **(fontdict if fontdict is not None else {}),
            **kwargs,
        }
        text = Text(x=x, y=y, text=s, **effective_kwargs)
        text.set_figure(self)
        text.stale_callback = _stale_figure_callback

        self.texts.append(text)
        text._remove_method = self.texts.remove
        self.stale = True
        return text

    def _set_artist_props(self, a):
        if a != self:
            a.set_figure(self)
        a.stale_callback = _stale_figure_callback
        a.set_transform(self.transFigure)

    @docstring.dedent_interpd
    def gca(self, **kwargs):
        """
        Get the current axes, creating one if necessary.

        The following kwargs are supported for ensuring the returned axes
        adheres to the given projection etc., and for axes creation if
        the active axes does not exist:

        %(Axes)s

        """
        ckey, cax = self._axstack.current_key_axes()
        # if there exists an axes on the stack see if it matches
        # the desired axes configuration
        if cax is not None:

            # if no kwargs are given just return the current axes
            # this is a convenience for gca() on axes such as polar etc.
            if not kwargs:
                return cax

            # if the user has specified particular projection detail
            # then build up a key which can represent this
            else:
                projection_class, _, key = \
                    self._process_projection_requirements(**kwargs)

                # let the returned axes have any gridspec by removing it from
                # the key
                ckey = ckey[1:]
                key = key[1:]

                # if the cax matches this key then return the axes, otherwise
                # continue and a new axes will be created
                if key == ckey and isinstance(cax, projection_class):
                    return cax
                else:
                    cbook._warn_external('Requested projection is different '
                                         'from current axis projection, '
                                         'creating new axis with requested '
                                         'projection.')

        # no axes found, so create one which spans the figure
        return self.add_subplot(1, 1, 1, **kwargs)

    def sca(self, a):
        """Set the current axes to be *a* and return *a*."""
        self._axstack.bubble(a)
        self._axobservers.process("_axes_change_event", self)
        return a

    def _gci(self):
        # Helper for `~matplotlib.pyplot.gci`.  Do not use elsewhere.
        """
        Get the current colorable artist.

        Specifically, returns the current `.ScalarMappable` instance (`.Image`
        created by `imshow` or `figimage`, `.Collection` created by `pcolor` or
        `scatter`, etc.), or *None* if no such instance has been defined.

        The current image is an attribute of the current axes, or the nearest
        earlier axes in the current figure that contains an image.

        Notes
        -----
        Historically, the only colorable artists were images; hence the name
        ``gci`` (get current image).
        """
        # Look first for an image in the current Axes:
        cax = self._axstack.current_key_axes()[1]
        if cax is None:
            return None
        im = cax._gci()
        if im is not None:
            return im

        # If there is no image in the current Axes, search for
        # one in a previously created Axes.  Whether this makes
        # sense is debatable, but it is the documented behavior.
        for ax in reversed(self.axes):
            im = ax._gci()
            if im is not None:
                return im
        return None

    def __getstate__(self):
        state = super().__getstate__()

        # The canvas cannot currently be pickled, but this has the benefit
        # of meaning that a figure can be detached from one canvas, and
        # re-attached to another.
        state.pop("canvas")

        # Set cached renderer to None -- it can't be pickled.
        state["_cachedRenderer"] = None

        # add version information to the state
        state['__mpl_version__'] = _mpl_version

        # check whether the figure manager (if any) is registered with pyplot
        from matplotlib import _pylab_helpers
        if getattr(self.canvas, 'manager', None) \
                in _pylab_helpers.Gcf.figs.values():
            state['_restore_to_pylab'] = True

        # set all the layoutbox information to None.  kiwisolver objects can't
        # be pickled, so we lose the layout options at this point.
        state.pop('_layoutbox', None)
        # suptitle:
        if self._suptitle is not None:
            self._suptitle._layoutbox = None

        return state

    def __setstate__(self, state):
        version = state.pop('__mpl_version__')
        restore_to_pylab = state.pop('_restore_to_pylab', False)

        if version != _mpl_version:
            cbook._warn_external(
                f"This figure was saved with matplotlib version {version} and "
                f"is unlikely to function correctly.")

        self.__dict__ = state

        # re-initialise some of the unstored state information
        FigureCanvasBase(self)  # Set self.canvas.
        self._layoutbox = None

        if restore_to_pylab:
            # lazy import to avoid circularity
            import matplotlib.pyplot as plt
            import matplotlib._pylab_helpers as pylab_helpers
            allnums = plt.get_fignums()
            num = max(allnums) + 1 if allnums else 1
            mgr = plt._backend_mod.new_figure_manager_given_figure(num, self)
            pylab_helpers.Gcf._set_new_active_manager(mgr)
            plt.draw_if_interactive()

        self.stale = True

    def add_axobserver(self, func):
        """Whenever the axes state change, ``func(self)`` will be called."""
        # Connect a wrapper lambda and not func itself, to avoid it being
        # weakref-collected.
        self._axobservers.connect("_axes_change_event", lambda arg: func(arg))

    def savefig(self, fname, *, transparent=None, **kwargs):
        """
        Save the current figure.

        Call signature::

          savefig(fname, dpi=None, facecolor='w', edgecolor='w',
                  orientation='portrait', papertype=None, format=None,
                  transparent=False, bbox_inches=None, pad_inches=0.1,
                  frameon=None, metadata=None)

        The available output formats depend on the backend being used.

        Parameters
        ----------
        fname : str or path-like or file-like
            A path, or a Python file-like object, or
            possibly some backend-dependent object such as
            `matplotlib.backends.backend_pdf.PdfPages`.

            If *format* is set, it determines the output format, and the file
            is saved as *fname*.  Note that *fname* is used verbatim, and there
            is no attempt to make the extension, if any, of *fname* match
            *format*, and no extension is appended.

            If *format* is not set, then the format is inferred from the
            extension of *fname*, if there is one.  If *format* is not
            set and *fname* has no extension, then the file is saved with
            :rc:`savefig.format` and the appropriate extension is appended to
            *fname*.

        Other Parameters
        ----------------
        dpi : float or 'figure', default: :rc:`savefig.dpi`
            The resolution in dots per inch.  If 'figure', use the figure's
            dpi value.

        quality : int, default: :rc:`savefig.jpeg_quality`
            Applicable only if *format* is 'jpg' or 'jpeg', ignored otherwise.

            The image quality, on a scale from 1 (worst) to 95 (best).
            Values above 95 should be avoided; 100 disables portions of
            the JPEG compression algorithm, and results in large files
            with hardly any gain in image quality.

            This parameter is deprecated.

        optimize : bool, default: False
            Applicable only if *format* is 'jpg' or 'jpeg', ignored otherwise.

            Whether the encoder should make an extra pass over the image
            in order to select optimal encoder settings.

            This parameter is deprecated.

        progressive : bool, default: False
            Applicable only if *format* is 'jpg' or 'jpeg', ignored otherwise.

            Whether the image should be stored as a progressive JPEG file.

            This parameter is deprecated.

        facecolor : color or 'auto', default: :rc:`savefig.facecolor`
            The facecolor of the figure.  If 'auto', use the current figure
            facecolor.

        edgecolor : color or 'auto', default: :rc:`savefig.edgecolor`
            The edgecolor of the figure.  If 'auto', use the current figure
            edgecolor.

        orientation : {'landscape', 'portrait'}
            Currently only supported by the postscript backend.

        papertype : str
            One of 'letter', 'legal', 'executive', 'ledger', 'a0' through
            'a10', 'b0' through 'b10'. Only supported for postscript
            output.

        format : str
            The file format, e.g. 'png', 'pdf', 'svg', ... The behavior when
            this is unset is documented under *fname*.

        transparent : bool
            If *True*, the axes patches will all be transparent; the
            figure patch will also be transparent unless facecolor
            and/or edgecolor are specified via kwargs.
            This is useful, for example, for displaying
            a plot on top of a colored background on a web page.  The
            transparency of these patches will be restored to their
            original values upon exit of this function.

        bbox_inches : str or `.Bbox`, default: :rc:`savefig.bbox`
            Bounding box in inches: only the given portion of the figure is
            saved.  If 'tight', try to figure out the tight bbox of the figure.

        pad_inches : float, default: :rc:`savefig.pad_inches`
            Amount of padding around the figure when bbox_inches is 'tight'.

        bbox_extra_artists : list of `~matplotlib.artist.Artist`, optional
            A list of extra artists that will be considered when the
            tight bbox is calculated.

        backend : str, optional
            Use a non-default backend to render the file, e.g. to render a
            png file with the "cairo" backend rather than the default "agg",
            or a pdf file with the "pgf" backend rather than the default
            "pdf".  Note that the default backend is normally sufficient.  See
            :ref:`the-builtin-backends` for a list of valid backends for each
            file format.  Custom backends can be referenced as "module://...".

        metadata : dict, optional
            Key/value pairs to store in the image metadata. The supported keys
            and defaults depend on the image format and backend:

            - 'png' with Agg backend: See the parameter ``metadata`` of
              `~.FigureCanvasAgg.print_png`.
            - 'pdf' with pdf backend: See the parameter ``metadata`` of
              `~.backend_pdf.PdfPages`.
            - 'svg' with svg backend: See the parameter ``metadata`` of
              `~.FigureCanvasSVG.print_svg`.
            - 'eps' and 'ps' with PS backend: Only 'Creator' is supported.

        pil_kwargs : dict, optional
            Additional keyword arguments that are passed to
            `PIL.Image.Image.save` when saving the figure.
        """

        kwargs.setdefault('dpi', mpl.rcParams['savefig.dpi'])
        if transparent is None:
            transparent = mpl.rcParams['savefig.transparent']

        if transparent:
            kwargs.setdefault('facecolor', 'none')
            kwargs.setdefault('edgecolor', 'none')
            original_axes_colors = []
            for ax in self.axes:
                patch = ax.patch
                original_axes_colors.append((patch.get_facecolor(),
                                             patch.get_edgecolor()))
                patch.set_facecolor('none')
                patch.set_edgecolor('none')

        self.canvas.print_figure(fname, **kwargs)

        if transparent:
            for ax, cc in zip(self.axes, original_axes_colors):
                ax.patch.set_facecolor(cc[0])
                ax.patch.set_edgecolor(cc[1])

    @docstring.dedent_interpd
    def colorbar(self, mappable, cax=None, ax=None, use_gridspec=True, **kw):
        """%(colorbar_doc)s"""
        if ax is None:
            ax = self.gca()

        # Store the value of gca so that we can set it back later on.
        current_ax = self.gca()

        if cax is None:
            if use_gridspec and isinstance(ax, SubplotBase)  \
                     and (not self.get_constrained_layout()):
                cax, kw = cbar.make_axes_gridspec(ax, **kw)
            else:
                cax, kw = cbar.make_axes(ax, **kw)

        # need to remove kws that cannot be passed to Colorbar
        NON_COLORBAR_KEYS = ['fraction', 'pad', 'shrink', 'aspect', 'anchor',
                             'panchor']
        cb_kw = {k: v for k, v in kw.items() if k not in NON_COLORBAR_KEYS}
        cb = cbar.colorbar_factory(cax, mappable, **cb_kw)

        self.sca(current_ax)
        self.stale = True
        return cb

    def subplots_adjust(self, left=None, bottom=None, right=None, top=None,
                        wspace=None, hspace=None):
        """
        Adjust the subplot layout parameters.

        Unset parameters are left unmodified; initial values are given by
        :rc:`figure.subplot.[name]`.

        Parameters
        ----------
        left : float, optional
            The position of the left edge of the subplots,
            as a fraction of the figure width.
        right : float, optional
            The position of the right edge of the subplots,
            as a fraction of the figure width.
        bottom : float, optional
            The position of the bottom edge of the subplots,
            as a fraction of the figure height.
        top : float, optional
            The position of the top edge of the subplots,
            as a fraction of the figure height.
        wspace : float, optional
            The width of the padding between subplots,
            as a fraction of the average axes width.
        hspace : float, optional
            The height of the padding between subplots,
            as a fraction of the average axes height.
        """
        if self.get_constrained_layout():
            self.set_constrained_layout(False)
            cbook._warn_external("This figure was using "
                                 "constrained_layout==True, but that is "
                                 "incompatible with subplots_adjust and or "
                                 "tight_layout: setting "
                                 "constrained_layout==False. ")
        self.subplotpars.update(left, bottom, right, top, wspace, hspace)
        for ax in self.axes:
            if not isinstance(ax, SubplotBase):
                # Check if sharing a subplots axis
                if isinstance(ax._sharex, SubplotBase):
                    ax._sharex.update_params()
                    ax.set_position(ax._sharex.figbox)
                elif isinstance(ax._sharey, SubplotBase):
                    ax._sharey.update_params()
                    ax.set_position(ax._sharey.figbox)
            else:
                ax.update_params()
                ax.set_position(ax.figbox)
        self.stale = True

    def ginput(self, n=1, timeout=30, show_clicks=True,
               mouse_add=MouseButton.LEFT,
               mouse_pop=MouseButton.RIGHT,
               mouse_stop=MouseButton.MIDDLE):
        """
        Blocking call to interact with a figure.

        Wait until the user clicks *n* times on the figure, and return the
        coordinates of each click in a list.

        There are three possible interactions:

        - Add a point.
        - Remove the most recently added point.
        - Stop the interaction and return the points added so far.

        The actions are assigned to mouse buttons via the arguments
        *mouse_add*, *mouse_pop* and *mouse_stop*.

        Parameters
        ----------
        n : int, default: 1
            Number of mouse clicks to accumulate. If negative, accumulate
            clicks until the input is terminated manually.
        timeout : float, default: 30 seconds
            Number of seconds to wait before timing out. If zero or negative
            will never timeout.
        show_clicks : bool, default: True
            If True, show a red cross at the location of each click.
        mouse_add : `.MouseButton` or None, default: `.MouseButton.LEFT`
            Mouse button used to add points.
        mouse_pop : `.MouseButton` or None, default: `.MouseButton.RIGHT`
            Mouse button used to remove the most recently added point.
        mouse_stop : `.MouseButton` or None, default: `.MouseButton.MIDDLE`
            Mouse button used to stop input.

        Returns
        -------
        list of tuples
            A list of the clicked (x, y) coordinates.

        Notes
        -----
        The keyboard can also be used to select points in case your mouse
        does not have one or more of the buttons.  The delete and backspace
        keys act like right clicking (i.e., remove last point), the enter key
        terminates input and any other key (not already used by the window
        manager) selects a point.
        """
        blocking_mouse_input = BlockingMouseInput(self,
                                                  mouse_add=mouse_add,
                                                  mouse_pop=mouse_pop,
                                                  mouse_stop=mouse_stop)
        return blocking_mouse_input(n=n, timeout=timeout,
                                    show_clicks=show_clicks)

    def waitforbuttonpress(self, timeout=-1):
        """
        Blocking call to interact with the figure.

        Wait for user input and return True if a key was pressed, False if a
        mouse button was pressed and None if no input was given within
        *timeout* seconds.  Negative values deactivate *timeout*.
        """
        blocking_input = BlockingKeyMouseInput(self)
        return blocking_input(timeout=timeout)

    def get_default_bbox_extra_artists(self):
        bbox_artists = [artist for artist in self.get_children()
                        if (artist.get_visible() and artist.get_in_layout())]
        for ax in self.axes:
            if ax.get_visible():
                bbox_artists.extend(ax.get_default_bbox_extra_artists())
        return bbox_artists

    def get_tightbbox(self, renderer, bbox_extra_artists=None):
        """
        Return a (tight) bounding box of the figure in inches.

        Artists that have ``artist.set_in_layout(False)`` are not included
        in the bbox.

        Parameters
        ----------
        renderer : `.RendererBase` subclass
            renderer that will be used to draw the figures (i.e.
            ``fig.canvas.get_renderer()``)

        bbox_extra_artists : list of `.Artist` or ``None``
            List of artists to include in the tight bounding box.  If
            ``None`` (default), then all artist children of each axes are
            included in the tight bounding box.

        Returns
        -------
        `.BboxBase`
            containing the bounding box (in figure inches).
        """

        bb = []
        if bbox_extra_artists is None:
            artists = self.get_default_bbox_extra_artists()
        else:
            artists = bbox_extra_artists

        for a in artists:
            bbox = a.get_tightbbox(renderer)
            if bbox is not None and (bbox.width != 0 or bbox.height != 0):
                bb.append(bbox)

        for ax in self.axes:
            if ax.get_visible():
                # some axes don't take the bbox_extra_artists kwarg so we
                # need this conditional....
                try:
                    bbox = ax.get_tightbbox(
                        renderer, bbox_extra_artists=bbox_extra_artists)
                except TypeError:
                    bbox = ax.get_tightbbox(renderer)
                bb.append(bbox)
        bb = [b for b in bb
              if (np.isfinite(b.width) and np.isfinite(b.height)
                  and (b.width != 0 or b.height != 0))]

        if len(bb) == 0:
            return self.bbox_inches

        _bbox = Bbox.union(bb)

        bbox_inches = TransformedBbox(_bbox, Affine2D().scale(1 / self.dpi))

        return bbox_inches

    def init_layoutbox(self):
        """Initialize the layoutbox for use in constrained_layout."""
        if self._layoutbox is None:
            self._layoutbox = layoutbox.LayoutBox(
                parent=None, name='figlb', artist=self)
            self._layoutbox.constrain_geometry(0., 0., 1., 1.)

    def execute_constrained_layout(self, renderer=None):
        """
        Use ``layoutbox`` to determine pos positions within axes.

        See also `.set_constrained_layout_pads`.
        """

        from matplotlib._constrained_layout import do_constrained_layout

        _log.debug('Executing constrainedlayout')
        if self._layoutbox is None:
            cbook._warn_external("Calling figure.constrained_layout, but "
                                 "figure not setup to do constrained layout. "
                                 " You either called GridSpec without the "
                                 "fig keyword, you are using plt.subplot, "
                                 "or you need to call figure or subplots "
                                 "with the constrained_layout=True kwarg.")
            return
        w_pad, h_pad, wspace, hspace = self.get_constrained_layout_pads()
        # convert to unit-relative lengths
        fig = self
        width, height = fig.get_size_inches()
        w_pad = w_pad / width
        h_pad = h_pad / height
        if renderer is None:
            renderer = layoutbox.get_renderer(fig)
        do_constrained_layout(fig, renderer, h_pad, w_pad, hspace, wspace)

    @cbook._delete_parameter("3.2", "renderer")
    def tight_layout(self, renderer=None, pad=1.08, h_pad=None, w_pad=None,
                     rect=None):
        """
        Adjust the padding between and around subplots.

        To exclude an artist on the axes from the bounding box calculation
        that determines the subplot parameters (i.e. legend, or annotation),
        set ``a.set_in_layout(False)`` for that artist.

        Parameters
        ----------
        renderer : subclass of `~.backend_bases.RendererBase`, optional
            Defaults to the renderer for the figure.  Deprecated.
        pad : float, default: 1.08
            Padding between the figure edge and the edges of subplots,
            as a fraction of the font size.
        h_pad, w_pad : float, default: *pad*
            Padding (height/width) between edges of adjacent subplots,
            as a fraction of the font size.
        rect : tuple (left, bottom, right, top), default: (0, 0, 1, 1)
            A rectangle in normalized figure coordinates into which the whole
            subplots area (including labels) will fit.

        See Also
        --------
        .Figure.set_tight_layout
        .pyplot.tight_layout
        """

        from .tight_layout import (
            get_renderer, get_subplotspec_list, get_tight_layout_figure)
        from contextlib import suppress
        subplotspec_list = get_subplotspec_list(self.axes)
        if None in subplotspec_list:
            cbook._warn_external("This figure includes Axes that are not "
                                 "compatible with tight_layout, so results "
                                 "might be incorrect.")

        if renderer is None:
            renderer = get_renderer(self)
        ctx = (renderer._draw_disabled()
               if hasattr(renderer, '_draw_disabled')
               else suppress())
        with ctx:
            kwargs = get_tight_layout_figure(
                self, self.axes, subplotspec_list, renderer,
                pad=pad, h_pad=h_pad, w_pad=w_pad, rect=rect)
        if kwargs:
            self.subplots_adjust(**kwargs)

    def align_xlabels(self, axs=None):
        """
        Align the ylabels of subplots in the same subplot column if label
        alignment is being done automatically (i.e. the label position is
        not manually set).

        Alignment persists for draw events after this is called.

        If a label is on the bottom, it is aligned with labels on axes that
        also have their label on the bottom and that have the same
        bottom-most subplot row.  If the label is on the top,
        it is aligned with labels on axes with the same top-most row.

        Parameters
        ----------
        axs : list of `~matplotlib.axes.Axes`
            Optional list of (or ndarray) `~matplotlib.axes.Axes`
            to align the xlabels.
            Default is to align all axes on the figure.

        See Also
        --------
        matplotlib.figure.Figure.align_ylabels
        matplotlib.figure.Figure.align_labels

        Notes
        -----
        This assumes that ``axs`` are from the same `.GridSpec`, so that
        their `.SubplotSpec` positions correspond to figure positions.

        Examples
        --------
        Example with rotated xtick labels::

            fig, axs = plt.subplots(1, 2)
            for tick in axs[0].get_xticklabels():
                tick.set_rotation(55)
            axs[0].set_xlabel('XLabel 0')
            axs[1].set_xlabel('XLabel 1')
            fig.align_xlabels()
        """
        if axs is None:
            axs = self.axes
        axs = np.ravel(axs)
        for ax in axs:
            _log.debug(' Working on: %s', ax.get_xlabel())
            rowspan = ax.get_subplotspec().rowspan
            pos = ax.xaxis.get_label_position()  # top or bottom
            # Search through other axes for label positions that are same as
            # this one and that share the appropriate row number.
            # Add to a grouper associated with each axes of siblings.
            # This list is inspected in `axis.draw` by
            # `axis._update_label_position`.
            for axc in axs:
                if axc.xaxis.get_label_position() == pos:
                    rowspanc = axc.get_subplotspec().rowspan
                    if (pos == 'top' and rowspan.start == rowspanc.start or
                            pos == 'bottom' and rowspan.stop == rowspanc.stop):
                        # grouper for groups of xlabels to align
                        self._align_xlabel_grp.join(ax, axc)

    def align_ylabels(self, axs=None):
        """
        Align the ylabels of subplots in the same subplot column if label
        alignment is being done automatically (i.e. the label position is
        not manually set).

        Alignment persists for draw events after this is called.

        If a label is on the left, it is aligned with labels on axes that
        also have their label on the left and that have the same
        left-most subplot column.  If the label is on the right,
        it is aligned with labels on axes with the same right-most column.

        Parameters
        ----------
        axs : list of `~matplotlib.axes.Axes`
            Optional list (or ndarray) of `~matplotlib.axes.Axes`
            to align the ylabels.
            Default is to align all axes on the figure.

        See Also
        --------
        matplotlib.figure.Figure.align_xlabels
        matplotlib.figure.Figure.align_labels

        Notes
        -----
        This assumes that ``axs`` are from the same `.GridSpec`, so that
        their `.SubplotSpec` positions correspond to figure positions.

        Examples
        --------
        Example with large yticks labels::

            fig, axs = plt.subplots(2, 1)
            axs[0].plot(np.arange(0, 1000, 50))
            axs[0].set_ylabel('YLabel 0')
            axs[1].set_ylabel('YLabel 1')
            fig.align_ylabels()
        """
        if axs is None:
            axs = self.axes
        axs = np.ravel(axs)
        for ax in axs:
            _log.debug(' Working on: %s', ax.get_ylabel())
            colspan = ax.get_subplotspec().colspan
            pos = ax.yaxis.get_label_position()  # left or right
            # Search through other axes for label positions that are same as
            # this one and that share the appropriate column number.
            # Add to a list associated with each axes of siblings.
            # This list is inspected in `axis.draw` by
            # `axis._update_label_position`.
            for axc in axs:
                if axc.yaxis.get_label_position() == pos:
                    colspanc = axc.get_subplotspec().colspan
                    if (pos == 'left' and colspan.start == colspanc.start or
                            pos == 'right' and colspan.stop == colspanc.stop):
                        # grouper for groups of ylabels to align
                        self._align_ylabel_grp.join(ax, axc)

    def align_labels(self, axs=None):
        """
        Align the xlabels and ylabels of subplots with the same subplots
        row or column (respectively) if label alignment is being
        done automatically (i.e. the label position is not manually set).

        Alignment persists for draw events after this is called.

        Parameters
        ----------
        axs : list of `~matplotlib.axes.Axes`
            Optional list (or ndarray) of `~matplotlib.axes.Axes`
            to align the labels.
            Default is to align all axes on the figure.

        See Also
        --------
        matplotlib.figure.Figure.align_xlabels

        matplotlib.figure.Figure.align_ylabels
        """
        self.align_xlabels(axs=axs)
        self.align_ylabels(axs=axs)

    def add_gridspec(self, nrows=1, ncols=1, **kwargs):
        """
        Return a `.GridSpec` that has this figure as a parent.  This allows
        complex layout of axes in the figure.

        Parameters
        ----------
        nrows : int, default: 1
            Number of rows in grid.

        ncols : int, default: 1
            Number or columns in grid.

        Returns
        -------
        `.GridSpec`

        Other Parameters
        ----------------
        **kwargs
            Keyword arguments are passed to `.GridSpec`.

        See Also
        --------
        matplotlib.pyplot.subplots

        Examples
        --------
        Adding a subplot that spans two rows::

            fig = plt.figure()
            gs = fig.add_gridspec(2, 2)
            ax1 = fig.add_subplot(gs[0, 0])
            ax2 = fig.add_subplot(gs[1, 0])
            # spans two rows:
            ax3 = fig.add_subplot(gs[:, 1])

        """

        _ = kwargs.pop('figure', None)  # pop in case user has added this...
        gs = GridSpec(nrows=nrows, ncols=ncols, figure=self, **kwargs)
        self._gridspecs.append(gs)
        return gs


def figaspect(arg):
    """
    Calculate the width and height for a figure with a specified aspect ratio.

    While the height is taken from :rc:`figure.figsize`, the width is
    adjusted to match the desired aspect ratio. Additionally, it is ensured
    that the width is in the range [4., 16.] and the height is in the range
    [2., 16.]. If necessary, the default height is adjusted to ensure this.

    Parameters
    ----------
    arg : float or 2d array
        If a float, this defines the aspect ratio (i.e. the ratio height /
        width).
        In case of an array the aspect ratio is number of rows / number of
        columns, so that the array could be fitted in the figure undistorted.

    Returns
    -------
    width, height
        The figure size in inches.

    Notes
    -----
    If you want to create an axes within the figure, that still preserves the
    aspect ratio, be sure to create it with equal width and height. See
    examples below.

    Thanks to Fernando Perez for this function.

    Examples
    --------
    Make a figure twice as tall as it is wide::

        w, h = figaspect(2.)
        fig = Figure(figsize=(w, h))
        ax = fig.add_axes([0.1, 0.1, 0.8, 0.8])
        ax.imshow(A, **kwargs)

    Make a figure with the proper aspect for an array::

        A = rand(5, 3)
        w, h = figaspect(A)
        fig = Figure(figsize=(w, h))
        ax = fig.add_axes([0.1, 0.1, 0.8, 0.8])
        ax.imshow(A, **kwargs)
    """

    isarray = hasattr(arg, 'shape') and not np.isscalar(arg)

    # min/max sizes to respect when autoscaling.  If John likes the idea, they
    # could become rc parameters, for now they're hardwired.
    figsize_min = np.array((4.0, 2.0))  # min length for width/height
    figsize_max = np.array((16.0, 16.0))  # max length for width/height

    # Extract the aspect ratio of the array
    if isarray:
        nr, nc = arg.shape[:2]
        arr_ratio = nr / nc
    else:
        arr_ratio = arg

    # Height of user figure defaults
    fig_height = mpl.rcParams['figure.figsize'][1]

    # New size for the figure, keeping the aspect ratio of the caller
    newsize = np.array((fig_height / arr_ratio, fig_height))

    # Sanity checks, don't drop either dimension below figsize_min
    newsize /= min(1.0, *(newsize / figsize_min))

    # Avoid humongous windows as well
    newsize /= max(1.0, *(newsize / figsize_max))

    # Finally, if we have a really funky aspect ratio, break it but respect
    # the min/max dimensions (we don't want figures 10 feet tall!)
    newsize = np.clip(newsize, figsize_min, figsize_max)
    return newsize

docstring.interpd.update(Figure=martist.kwdoc(Figure))
