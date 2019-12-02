from __future__ import absolute_import

import sys

import param

from panel.param import ParamMethod
from panel.pane import PaneBase
from panel.layout import Row, Tabs
from panel.util import param_name

from .core import Element, Overlay
from .element import Path, Polygons, Points, Table
from .plotting.links import VertexTableLink, DataLink, SelectionLink
from .streams import BoxEdit, PolyDraw, PolyEdit, Selection1D, PointDraw


def preprocess(function, current=[]):
    """
    Turns a param.depends watch call into a preprocessor method, i.e.
    skips all downstream events triggered by it.

    NOTE: This is a temporary hack while the addition of preprocessors
          in param is under discussion. This only works for the first
          method which depends on a particular parameter.

          (see https://github.com/pyviz/param/issues/332)
    """
    def inner(*args, **kwargs):
        self = args[0]
        self.param._BATCH_WATCH = True
        function(*args, **kwargs)
        self.param._BATCH_WATCH = False
        self.param._watchers = []
        self.param._events = []
    return inner


class AnnotationManager(param.Parameterized):
    """
    The AnnotationManager allows combining any number of Annotators
    and elements together into a single linked, displayable panel
    object.

    The manager consists of two main components, the `plot` to
    annotate and the `editor` containing the annotator tables. These
    are combined into a `layout` by default which are displayed when
    the manager is visualized.
    """

    layers = param.List(default=[], doc="""
        Annotators and/or Elements to manage.""")

    opts = param.Dict(default={'responsive': True, 'min_height': 400}, doc="""
        The options to apply to the plot layers.""")

    table_opts = param.Dict(default={'width': 400}, doc="""
        The options to apply to editor tables.""")

    def __init__(self, layers=[], **params):
        super(AnnotationManager, self).__init__(**params)
        for layer in layers:
            self.add_layer(layer)
        self.plot = ParamMethod(self._get_plot)
        self.editor = Tabs()
        self._update_editor()
        self.layout = Row(self.plot, self.editor, sizing_mode='stretch_width')

    @param.depends('layers')
    def _get_plot(self):
        layers = []
        for layer in self.layers:
            if isinstance(layer, Annotator):
                layers.append(layer.element)
            else:
                layers.append(layer)
        overlay = Overlay(layers)
        if len(overlay):
            overlay = overlay.collate()
        return overlay.opts(**self.opts)

    @param.depends('layers', watch=True)
    def _update_editor(self):
        tables = []
        for layer in self.layers:
            if not isinstance(layer, Annotator):
                continue
            tables += [(name, t.opts(**self.table_opts)) for name, t in layer._tables]
        self.editor[:] = tables

    def _repr_mimebundle_(self, include=None, exclude=None):
        return self.layout._repr_mimebundle_(include, exclude)

    def add_layer(self, layer):
        """Adds a layer to the manager.

        Adds an Annotator or Element to the layers managed by the
        annotation manager.

        Args:
            layer (Annotator or Element): Layer to add to the manager
        """
        if isinstance(layer, Annotator):
            layer.param.watch(lambda event: self.param.trigger('layers'), 'element')
        elif not isinstance(layer, Element):
            raise ValueError('Annotator layer must be a Annotator subclass '
                             'or a HoloViews/GeoViews element.')
        self.layers.append(layer)
        self.param.trigger('layers')



class Annotator(PaneBase):
    """
    An Annotator allows drawing, editing and annotating a specific
    type of element. Each Annotator consists of the `plot` to draw and
    edit the element and the `editor`, which contains a list of tables,
    which make it possible to annotate each object in the element with
    additional properties defined in the `annotations`.
    """

    annotations = param.ClassSelector(default=[], class_=(dict, list), doc="""
        Annotations to associate with each object.""")

    object = param.ClassSelector(class_=Element, doc="""
        The Element to edit and annotate.""")

    num_objects = param.Integer(default=None, bounds=(0, None), doc="""
        The maximum number of objects to draw.""")

    opts = param.Dict(default={'responsive': True, 'min_height': 400,
                               'padding': 0.1}, doc="""
        Opts to apply to the element.""")

    table_transforms = param.HookList(default=[], doc="""
        Transform(s) to apply to element when converting data to Table.
        The functions should accept the Annotator and the transformed
        element as input.""")

    table_opts = param.Dict(default={'editable': True}, doc="""
        Opts to apply to the editor table(s).""")

    # Once generic editing tools are merged into bokeh this could
    # include snapshot, restore and clear tools
    _tools = []

    # Allows patching on custom behavior
    _extra_opts = {}

    priority = 0.7

    @classmethod
    def applies(cls, obj):
        if 'holoviews' not in sys.modules:
            return False
        return isinstance(obj, cls.param.object.class_)

    def select(self, selector=None):
        return self.layout.select(selector)

    @property
    def _element_type(self):
        return self.param.object.class_

    @property
    def _object_name(self):
        return self._element_type.__name__

    def __init__(self, object=None, **params):
        super(Annotator, self).__init__(object, **params)
        self._tables = []
        self.editor = Tabs()
        self._selection = Selection1D()
        self._initialize(object)
        self.plot = ParamMethod(self._get_plot)
        self.layout[:] = [self.plot, self.editor]

    def _get_model(self, doc, root=None, parent=None, comm=None):
        return self.layout._get_model(doc, root, parent, comm)

    @param.depends('annotations', 'object', 'num_objects', 'opts', 'table_opts', watch=True)
    @preprocess
    def _initialize(self, object=None):
        """
        Initializes the object ready for annotation.
        """
        object = self.object if object is None else object
        self._init_element(object)
        self._init_table()
        self._selection.source = self.object
        self.editor[:] = self._tables
        self._stream.add_subscriber(self._update_element)

    @param.depends('object')
    def _get_plot(self):
        return self.object

    def _update_element(self, data=None):
        with param.discard_events(self):
            self.object = self._stream.element

    def _table_data(self):
        """
        Returns data used to initialize the table.
        """
        object = self.object
        for transform in self.table_transforms:
            object = transform(object)
        return object

    def _init_element(self, object):
        """
        Subclasses should implement this method.
        """

    def _init_table(self):
        """
        Subclasses should implement this method.
        """

    @property
    def selected(self):
        """
        Subclasses should return a new object containing currently selected objects.
        """


class PathAnnotator(Annotator):
    """
    Annotator which allows drawing and editing Paths and associating
    values with each path and each vertex of a path using a table.
    """

    edit_vertices = param.Boolean(default=True, doc="""
        Whether to add tool to edit vertices.""")

    object = param.ClassSelector(class_=Path, doc="""
        Path object to edit and annotate.""")

    show_vertices = param.Boolean(default=True, doc="""
        Whether to show vertices when drawing the Path.""")

    vertex_annotations = param.ClassSelector(default=[], class_=(dict, list), doc="""
        Columns to annotate the Polygons with.""")

    vertex_style = param.Dict(default={'nonselection_alpha': 0.5}, doc="""
        Options to apply to vertices during drawing and editing.""")

    _vertex_table_link = VertexTableLink

    def _init_element(self, element=None):
        if element is None or not isinstance(element, self._element_type):
            datatype = list(self._element_type.datatype)
            datatype.remove('multitabular')
            datatype.append('multitabular')
            element = self._element_type(element, datatype=datatype)

        # Add annotation columns to poly data
        validate = []
        for col in self.annotations:
            if col in element:
                validate.append(col)
                continue
            init = self.annotations[col]() if isinstance(self.annotations, dict) else ''
            element = element.add_dimension(col, 0, init, True)
        for col in self.vertex_annotations:
            if col in element:
                continue
            elif isinstance(self.vertex_annotations, dict):
                init = self.vertex_annotations[col]()
            else:
                init = ''
            element = element.add_dimension(col, 0, init, True)

        # Validate annotations
        poly_data = {c: self.element.dimension_values(c, expanded=False)
                     for c in validate}
        if validate and len(set(len(v) for v in poly_data.values())) != 1:
            raise ValueError('annotations must refer to value dimensions '
                             'which vary per path while at least one of '
                             '%s varies by vertex.' % validate)

        # Add options to element
        tools = [tool() for tool in self._tools]
        opts = dict(tools=tools, color_index=None, **self.opts)
        opts.update(self._extra_opts)
        self.object = element.options(**opts)

    def _init_table(self):
        name = param_name(self.name)
        self._stream = PolyDraw(
            source=self.object, data={}, num_objects=self.num_objects,
            show_vertices=self.show_vertices, tooltip='%s Tool' % name,
            vertex_style=self.vertex_style
        )
        if self.edit_vertices:
            self._vertex_stream = PolyEdit(
                source=self.object, tooltip='%s Edit Tool' % name,
                vertex_style=self.vertex_style,
            )

        annotations = list(self.annotations)
        table_data = self._table_data().split()
        table_data = {a: [d.dimension_values(a, expanded=False)[0] for d in table_data]
                      for a in annotations}
        self._table = Table(table_data, annotations, []).opts(**self.table_opts)
        self._link = DataLink(self.object, self._table)
        self._vertex_table = Table(
            [], self.object.kdims, list(self.vertex_annotations)
        ).opts(**self.table_opts)
        self._vertex_link = self._vertex_table_link(self.object, self._vertex_table)
        self._tables = [
            ('%s' % name, self._table),
            ('%s Vertices' % name, self._vertex_table)
        ]

    def _update_element(self, data=None):
        element = self._poly_stream.element
        if (element.interface.datatype == 'multitabular' and
            element.data and isinstance(element.data[0], dict)):
            for path in element.data:
                for col in self.annotations:
                    if len(path[col]):
                        path[col] = path[col][0]
        with param.discard_events(self):
            self.object = element

    @property
    def selected(self):
        index = self._selection.index
        data = [p for i, p in enumerate(self._stream.element.split()) if i in index]
        return self.output.clone(data)


class PolyAnnotator(PathAnnotator):
    """
    Annotator which allows drawing and editing Polygons and associating
    values with each polygon and each vertex of a Polygon using a table.
    """

    object = param.ClassSelector(class_=Polygons, doc="""
         Polygon element to edit and annotate.""")


class PointAnnotator(Annotator):
    """
    Annotator which allows drawing and editing Points and associating
    values with each point using a table.
    """

    object = param.ClassSelector(class_=Points, doc="""
        Points element to edit and annotate.""")

    opts = param.Dict(default={'responsive': True, 'min_height': 400,
                               'padding': 0.1, 'size': 10}, doc="""
        Opts to apply to the element.""")

    # Link between Points and Table
    _point_table_link = DataLink

    def _init_element(self, object):
        if object is None or not isinstance(object, self._element_type):
            object = self._element_type(object)

        # Add annotations
        for col in self.annotations:
            if col in object:
                continue
            init = self.annotations[col] if isinstance(self.annotations, dict) else None
            object = object.add_dimension(col, 0, init, True)

        # Add options
        tools = [tool() for tool in self._tools]
        opts = dict(tools=tools, **self.opts)
        opts.update(self._extra_opts)
        self.object = object.options(**opts)

    def _init_table(self):
        name = param_name(self.name)
        self._stream = PointDraw(
            source=self.object, data={}, num_objects=self.num_objects,
            tooltip='%s Tool' % name
        )
        table_data = self._table_data()
        self._table = Table(table_data).opts(**self.table_opts)
        self._point_link = self._point_table_link(self.object, self._table)
        self._point_selection_link = SelectionLink(self.object, self._table)
        self._tables = [('%s' % name, self._table)]

    @property
    def selected(self):
        return self.object.iloc[self._point_selection.index]


class BoxAnnotator(Annotator):

    object = param.ClassSelector(class_=Path, doc="""
        Points element to edit and annotate.""")

    def _init_element(self, element):
        if element is None or not isinstance(element, self._element_type):
            element = self._element_type(element)

        # Add annotations
        for col in self.annotations:
            if col in element:
                continue
            init = self.annotations[col] if isinstance(self.annotations, dict) else None
            element = element.add_dimension(col, 0, init, True)

        # Add options
        tools = [tool() for tool in self._tools]
        opts = dict(tools=tools, **self.opts)
        opts.update(self._extra_opts)
        self.object = element.options(**opts)

    def _init_table(self):
        name = param_name(self.name)
        self._stream = BoxEdit(
            source=self.object, data={}, num_objects=self.num_objects,
            tooltip='%s Tool' % name
        )
        table_data = self._table_data()
        self._table = Table(table_data, [], self.annotations).opts(**self.table_opts)
        self._point_link = DataLink(self.object, self._table)
        self._tables = [('%s' % name, self._table)]

    @property
    def selected(self):
        index = self._selection.index
        data = [p for i, p in enumerate(self._stream.element.split()) if i in index]
        return self.output.clone(data)
