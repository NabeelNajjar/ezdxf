# Copyright (c) 2020, Matthew Broadway
# License: MIT License
import math
from typing import Optional, Iterable, Dict, Sequence, Union, Tuple
import warnings
from collections import defaultdict
from functools import lru_cache
from PyQt5 import QtCore as qc, QtGui as qg, QtWidgets as qw

from ezdxf.addons.drawing.backend import Backend, prepare_string_for_rendering
from ezdxf.addons.drawing.text import FontMeasurements
from ezdxf.addons.drawing.type_hints import Color
from ezdxf.addons.drawing.properties import Properties
from ezdxf.addons.drawing import fonts
from ezdxf.math import Vector, Matrix44
from ezdxf.render import Path, Command
from ezdxf.units import IMPERIAL_UNITS


class _Point(qw.QAbstractGraphicsShapeItem):
    """ a point which is drawn 'cosmetically' (scale depends on view) """

    def __init__(self, x: float, y: float, radius: float, brush: qg.QBrush):
        super().__init__()
        self.pos = qc.QPointF(x, y)
        self.radius = radius
        self.setPen(qg.QPen(qc.Qt.NoPen))
        self.setBrush(brush)

    def paint(self, painter: qg.QPainter, option: qw.QStyleOptionGraphicsItem,
              widget: Optional[qw.QWidget] = None) -> None:
        view_scale = _get_x_scale(painter.transform())
        radius = self.radius / view_scale

        painter.setBrush(self.brush())
        painter.setPen(qc.Qt.NoPen)
        painter.drawEllipse(self.pos, radius, radius)

    def boundingRect(self) -> qc.QRectF:
        return qc.QRectF(self.pos, qc.QSizeF(1, 1))


# The key used to store the dxf entity corresponding to each graphics element
CorrespondingDXFEntity = 0
CorrespondingDXFParentStack = 1

PYQT_DEFAULT_PARAMS = {
    'point_size': 1.0,
    # For my taste without scaling the default line width looks to thin:
    'lineweight_scaling': 2.0,
}


class PyQtBackend(Backend):
    def __init__(self,
                 scene: Optional[qw.QGraphicsScene] = None,
                 point_radius=None,  # deprecated
                 *,
                 use_text_cache: bool = True,
                 debug_draw_rect: bool = False,
                 params: Dict = None):
        params_ = dict(PYQT_DEFAULT_PARAMS)
        params_.update(params or {})
        super().__init__(params_)
        if point_radius is not None:
            self.point_size = point_radius * 2.0
            warnings.warn(
                'The "point_radius" argument is deprecated use the params  dict '
                'to pass arguments to the PyQtBackend, '
                'will be removed in v0.16.', DeprecationWarning)

        self._scene = scene
        self._color_cache = {}
        self._no_line = qg.QPen(qc.Qt.NoPen)
        self._no_fill = qg.QBrush(qc.Qt.NoBrush)
        self._text_renderer = TextRenderer(qg.QFont(), use_text_cache)
        self._debug_draw_rect = debug_draw_rect
        self._dash_pattern_cache: Dict[int: Tuple[float, ...]] = dict()

    def set_scene(self, scene: qw.QGraphicsScene):
        self._scene = scene

    def clear_text_cache(self):
        self._text_renderer.clear_cache()

    def _get_color(self, color: Color) -> qg.QColor:
        qt_color = self._color_cache.get(color, None)
        if qt_color is None:
            if len(color) == 7:
                qt_color = qg.QColor(color)  # '#RRGGBB'
            elif len(color) == 9:
                rgb = color[1:7]
                alpha = color[7:9]
                qt_color = qg.QColor(f'#{alpha}{rgb}')  # '#AARRGGBB'
            else:
                raise TypeError(color)

            self._color_cache[color] = qt_color
        return qt_color

    def _get_pen(self, properties: Properties) -> qg.QPen:
        # properties.lineweight is in mm like 0.25mm (default lineweight)
        # mm to pixel for 72 dpi: 1px is 0.3527 mm
        # Note that a pen with zero width is equivalent to a cosmetic pen with a
        # width of 1 pixel (lineweight_scaling=0).
        px = properties.lineweight / 0.3527 * self.lineweight_scaling
        pen = qg.QPen(self._get_color(properties.color), px)
        # Use constant width in pixel:
        pen.setCosmetic(True)
        pen.setJoinStyle(qc.Qt.RoundJoin)
        if len(properties.linetype_pattern) > 1 and self.linetype_scaling != 0:
            # The dash pattern is specified in units of the pens width; e.g. a
            # dash of length 5 in width 10 is 50 pixels long.
            pattern_factor = self._get_line_pattern_factor(properties.units)
            pen.setDashPattern(
                self._get_dash_pattern(
                    properties.linetype_pattern,
                    properties.linetype_scale * pattern_factor
                ))
        return pen

    def _get_line_pattern_factor(self, units: int) -> float:
        # do not cache!
        scale = self.linetype_scaling or 1.0
        # just guessing: this values assume a cosmetic pen!
        return (750 if units in IMPERIAL_UNITS else 30) * scale

    def _get_dash_pattern(self, pattern: Tuple[float, ...],
                          scale: float) -> Tuple[float, ...]:
        hash_key = hash((pattern, scale))
        try:
            dashes = self._dash_pattern_cache[hash_key]
        except KeyError:
            end = len(pattern)
            if end % 2:  # grant even number, last dash is ignored
                end = -1
            min_length = self.min_dash_length
            dashes = tuple(
                max(dash * scale, min_length) for dash in pattern[:end]
            )
            self._dash_pattern_cache[hash_key] = dashes
        return dashes

    def _get_brush(self, properties: Properties) -> qg.QBrush:
        if properties.filling:
            return qg.QBrush(
                self._get_color(properties.color),
                qc.Qt.SolidPattern
            )
        else:
            return self._no_fill

    def _set_item_data(self, item: qw.QGraphicsItem) -> None:
        item.setData(CorrespondingDXFEntity, self.current_entity)
        parent_stack = tuple(e for e, props in self.entity_stack[:-1])
        item.setData(CorrespondingDXFParentStack, parent_stack)

    def set_background(self, color: Color):
        self._scene.setBackgroundBrush(qg.QBrush(self._get_color(color)))

    def draw_point(self, pos: Vector, properties: Properties) -> None:
        brush = qg.QBrush(self._get_color(properties.color), qc.Qt.SolidPattern)
        item = _Point(pos.x, pos.y, self.point_size * 0.5, brush)
        self._set_item_data(item)
        self._scene.addItem(item)

    def draw_line(self, start: Vector, end: Vector,
                  properties: Properties) -> None:
        item = self._scene.addLine(
            start.x, start.y, end.x, end.y,
            self._get_pen(properties)
        )
        self._set_item_data(item)

    def draw_path(self, path: Path, properties: Properties) -> None:
        qt_path = qg.QPainterPath()
        _extend_qt_path(qt_path, path)
        item = self._scene.addPath(
            qt_path,
            self._get_pen(properties),
            self._no_fill,
        )
        self._set_item_data(item)

    def draw_filled_paths(self, paths: Sequence[Path], holes: Sequence[Path],
                          properties: Properties) -> None:
        if self.show_hatch == 0:
            return
        qt_path = qg.QPainterPath()
        for path in paths:
            _extend_qt_path(qt_path, path.counter_clockwise())
        for path in holes:
            _extend_qt_path(qt_path, path.clockwise())
        item = self._scene.addPath(
            qt_path,
            self._get_pen(properties),
            self._get_brush(properties),
        )
        self._set_item_data(item)

    def draw_filled_polygon(self, points: Iterable[Vector],
                            properties: Properties) -> None:
        brush = self._get_brush(properties)
        polygon = qg.QPolygonF()
        for p in points:
            polygon.append(qc.QPointF(p.x, p.y))
        item = self._scene.addPolygon(polygon, self._no_line, brush)
        self._set_item_data(item)

    def draw_text(self, text: str, transform: Matrix44, properties: Properties,
                  cap_height: float) -> None:
        if not text.strip():
            return  # no point rendering empty strings
        text = prepare_string_for_rendering(text, self.current_entity.dxftype())
        qfont = self.get_qfont(properties.font)
        scale = self._text_renderer.get_scale(cap_height, qfont)
        transform = Matrix44.scale(scale, -scale, 0) @ transform

        path = self._text_renderer.get_text_path(text, qfont)
        path = _matrix_to_qtransform(transform).map(path)
        item = self._scene.addPath(path, self._no_line,
                                   self._get_color(properties.color))
        self._set_item_data(item)

    @lru_cache(maxsize=256)  # fonts.Font is a named tuple
    def get_qfont(self, font: fonts.Font) -> qg.QFont:
        qfont = self._text_renderer.default_font
        if font:
            family = font.family
            italic = "italic" in font.style.lower()
            weight = _map_weight(font.weight)
            qfont = qg.QFont(family, weight=weight, italic=italic)
        return qfont

    def get_font_measurements(self, cap_height: float,
                              font: fonts.Font = None) -> FontMeasurements:
        qfont = self.get_qfont(font)
        return self._text_renderer.get_font_measurements(
            qfont).scale_from_baseline(desired_cap_height=cap_height)

    def get_text_line_width(self, text: str, cap_height: float,
                            font: fonts.Font = None) -> float:
        if not text.strip():
            return 0

        dxftype = self.current_entity.dxftype() if self.current_entity else 'TEXT'
        text = prepare_string_for_rendering(text, dxftype)
        qfont = self.get_qfont(font)
        scale = self._text_renderer.get_scale(cap_height, qfont)
        return self._text_renderer.get_text_rect(text, qfont).right() * scale

    def clear(self) -> None:
        self._scene.clear()

    def finalize(self) -> None:
        super().finalize()
        self._scene.setSceneRect(self._scene.itemsBoundingRect())
        if self._debug_draw_rect:
            properties = Properties()
            properties.color = '#000000'
            self._scene.addRect(
                self._scene.sceneRect(),
                self._get_pen(properties),
                self._no_fill
            )


def _extend_qt_path(qt_path: qg.QPainterPath, path: Path) -> None:
    start = path.start
    qt_path.moveTo(start.x, start.y)
    for cmd in path:
        if cmd.type == Command.LINE_TO:
            end = cmd.end
            qt_path.lineTo(end.x, end.y)
        elif cmd.type == Command.CURVE_TO:
            end = cmd.end
            ctrl1 = cmd.ctrl1
            ctrl2 = cmd.ctrl2
            qt_path.cubicTo(
                ctrl1.x, ctrl1.y, ctrl2.x, ctrl2.y, end.x, end.y
            )
        else:
            raise ValueError(f'Unknown path command: {cmd.type}')


# https://doc.qt.io/qt-5/qfont.html#Weight-enum
# QFont::Thin	0	0
# QFont::ExtraLight	12	12
# QFont::Light	25	25
# QFont::Normal	50	50
# QFont::Medium	57	57
# QFont::DemiBold	63	63
# QFont::Bold	75	75
# QFont::ExtraBold	81	81
# QFont::Black	87	87
def _map_weight(weight: Union[str, int]) -> int:
    if isinstance(weight, str):
        weight = fonts.weight_name_to_value(weight)
    value = int((weight / 10) + 10)  # normal: 400 -> 50
    return min(max(0, value), 99)


def _get_x_scale(t: qg.QTransform) -> float:
    return math.sqrt(t.m11() * t.m11() + t.m21() * t.m21())


def _matrix_to_qtransform(matrix: Matrix44) -> qg.QTransform:
    """ Qt also uses row-vectors so the translation elements are placed in the
    bottom row.

    This is only a simple conversion which assumes that although the
    transformation is 4x4,it does not involve the z axis.

    A more correct transformation could be implemented like so:
    https://stackoverflow.com/questions/10629737/convert-3d-4x4-rotation-matrix-into-2d
    """
    return qg.QTransform(*matrix.get_2d_transformation())


class TextRenderer:
    def __init__(self, font: qg.QFont, use_cache: bool):
        self._default_font = font
        self._use_cache = use_cache

        # Each font has its own text path cache
        # key is hash(QFont)
        self._text_path_cache: Dict[
            int, Dict[str, qg.QPainterPath]] = defaultdict(dict)

        # Each font has its own font measurements cache
        # key is hash(QFont)
        self._font_measurement_cache: Dict[
            int, FontMeasurements] = {}

    @property
    def default_font(self) -> qg.QFont:
        return self._default_font

    def clear_cache(self):
        self._text_path_cache.clear()

    def get_scale(self, desired_cap_height: float, font: qg.QFont) -> float:
        measurements = self.get_font_measurements(font)
        return desired_cap_height / measurements.cap_height

    def get_font_measurements(self, font: qg.QFont) -> FontMeasurements:
        # None is the default font.
        key = hash(font)  # good hash?
        measurements = self._font_measurement_cache.get(key)
        if measurements is None:
            upper_x = self.get_text_rect('X', font)
            lower_x = self.get_text_rect('x', font)
            lower_p = self.get_text_rect('p', font)
            baseline = lower_x.bottom()
            measurements = FontMeasurements(
                baseline=baseline,
                cap_height=baseline - upper_x.top(),
                x_height=baseline - lower_x.top(),
                descender_height=lower_p.bottom() - baseline,
            )
            self._font_measurement_cache[key] = measurements
        return measurements

    def get_text_path(self, text: str, font: qg.QFont) -> qg.QPainterPath:
        # None is the default font
        cache = self._text_path_cache[hash(font)]  # defaultdict(dict)
        path = cache.get(text, None)
        if path is None:
            if font is None:
                font = self._default_font
            path = qg.QPainterPath()
            path.addText(0, 0, font, text)
            if self._use_cache:
                cache[text] = path
        return path

    def get_text_rect(self, text: str, font: qg.QFont) -> qc.QRectF:
        # no point caching the bounding rect calculation, it is very cheap
        return self.get_text_path(text, font).boundingRect()
