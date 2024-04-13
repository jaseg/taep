import math
import re
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LengthUnit:
    """ Convenience length unit class. Used in :py:class:`.GraphicObject` and :py:class:`.Aperture` to store lenght
    information. Provides a number of useful unit conversion functions.

    Singleton, use only global instances ``utils.MM`` and ``utils.Inch``.
    """

    name: str
    shorthand: str
    this_in_mm: float

    def convert_from(self, unit, value):
        """ Convert ``value`` from ``unit`` into this unit.

        :param unit: ``MM``, ``Inch`` or one of the strings ``"mm"`` or ``"inch"``
        :param float value: 
        :rtype: float
        """

        if isinstance(unit, str):
            unit = units[unit]

        if unit == self or unit is None or value is None:
            return value

        return value * unit.this_in_mm / self.this_in_mm

    def convert_to(self, unit, value):
        """ :py:meth:`.LengthUnit.convert_from` but in reverse. """

        if isinstance(unit, str):
            unit = to_unit(unit)

        if unit is None:
            return value

        return unit.convert_from(self, value)

    def convert_bounds_from(self, unit, value):
        """ :py:meth:`.LengthUnit.convert_from` but for ((min_x, min_y), (max_x, max_y)) bounding box tuples. """

        if value is None:
            return None

        (min_x, min_y), (max_x, max_y) = value
        min_x = self.convert_from(unit, min_x)
        min_y = self.convert_from(unit, min_y)
        max_x = self.convert_from(unit, max_x)
        max_y = self.convert_from(unit, max_y)
        return (min_x, min_y), (max_x, max_y)

    def convert_bounds_to(self, unit, value):
        """ :py:meth:`.LengthUnit.convert_to` but for ((min_x, min_y), (max_x, max_y)) bounding box tuples. """

        if value is None:
            return None

        (min_x, min_y), (max_x, max_y) = value
        min_x = self.convert_to(unit, min_x)
        min_y = self.convert_to(unit, min_y)
        max_x = self.convert_to(unit, max_x)
        max_y = self.convert_to(unit, max_y)
        return (min_x, min_y), (max_x, max_y)

    def format(self, value):
        """ Return a human-readdable string representing value in this unit.

        :param float value:
        :returns: something like "3mm"
        :rtype: str
        """

        return f'{value:.3f}{self.shorthand}' if value is not None else ''

    def __call__(self, value, unit):
        """ Convenience alias for :py:meth:`.LengthUnit.convert_from` """
        return self.convert_from(unit, value)

    def __eq__(self, other):
        if isinstance(other, str):
            return other.lower() in (self.name, self.shorthand)
        else:
            return id(self) == id(other)

    # This class is a singleton, we don't want copies around
    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __str__(self):
        return self.shorthand

    def __repr__(self):
        return f'<LengthUnit {self.name}>'


MILLIMETERS_PER_INCH = 25.4
Inch = LengthUnit('inch', 'in', MILLIMETERS_PER_INCH)
MM = LengthUnit('millimeter', 'mm', 1)
units = {'inch': Inch, 'mm': MM, None: None}


class Tag:
    """ Helper class to ease creation of SVG. All API functions that create SVG allow you to substitute this with your
    own implementation by passing a ``tag`` parameter. """

    def __init__(self, name, children=None, root=False, **attrs):
        if (fill := attrs.get('fill')) and isinstance(fill, tuple):
            attrs['fill'], attrs['fill-opacity'] = fill
        if (stroke := attrs.get('stroke')) and isinstance(stroke, tuple):
            attrs['stroke'], attrs['stroke-opacity'] = stroke
        self.name, self.attrs = name, attrs
        self.children = children or []
        self.root = root

    def __str__(self):
        prefix = '<?xml version="1.0" encoding="utf-8"?>\n' if self.root else ''
        opening = ' '.join([self.name] + [f'{key.replace("__", ":").replace("_", "-")}="{value}"' for key, value in self.attrs.items()])
        if self.children:
            children = '\n'.join(textwrap.indent(str(c), '  ') for c in self.children)
            return f'{prefix}<{opening}>\n{children}\n</{self.name}>'
        else:
            return f'{prefix}<{opening}/>'


def svg_rotation(angle_rad, cx=0, cy=0):
    if math.isclose(angle_rad, 0.0, abs_tol=1e-3):
        return {}
    else:
        return {'transform': f'rotate({float(math.degrees(angle_rad)):.4} {float(cx):.6} {float(cy):.6})'}

def setup_svg(tags, bounds, margin=0, arg_unit=MM, svg_unit=MM, pagecolor='white', tag=Tag, inkscape=False):
    (min_x, min_y), (max_x, max_y) = bounds

    if margin:
        margin = svg_unit(margin, arg_unit)
        min_x -= margin
        min_y -= margin
        max_x += margin
        max_y += margin

    w, h = max_x - min_x, max_y - min_y
    w = 1.0 if math.isclose(w, 0.0) else w
    h = 1.0 if math.isclose(h, 0.0) else h

    if inkscape:
        tags.insert(0, tag('sodipodi:namedview', [], id='namedview1', pagecolor=pagecolor,
                inkscape__document_units=svg_unit.shorthand))
        namespaces = dict(
            xmlns="http://www.w3.org/2000/svg",
            xmlns__xlink="http://www.w3.org/1999/xlink",
            xmlns__sodipodi='http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd',
            xmlns__inkscape='http://www.inkscape.org/namespaces/inkscape')

    else:
        namespaces = dict(
            xmlns="http://www.w3.org/2000/svg",
            xmlns__xlink="http://www.w3.org/1999/xlink")

    svg_unit = 'in' if svg_unit == 'inch' else 'mm'
    # TODO export apertures as <uses> where reasonable.
    return tag('svg', tags,
            width=f'{w}{svg_unit}', height=f'{h}{svg_unit}',
            viewBox=f'{min_x} {min_y} {w} {h}',
            style=f'background-color:{pagecolor}',
            **namespaces,
            root=True)


class Transform:
    xform_re = r'((matrix|translate|scale|rotate|skewX|skewY)\(([-0-9. ]+)\))|(.+)'

    def __init__(self, a=1, b=0, c=0, d=1, e=0, f=0):
        # Reference: https://developer.mozilla.org/en-US/docs/Web/SVG/Attribute/transform
        self.mat = (a, b, c, d, e, f)

    def __mul__(self, other):
        a1, b1, c1, d1, e1, f1 = self.mat
        a2, b2, c2, d2, e2, f2 = other.mat

        a = a1*a2 + c1*b2
        b = d1*b2 + b1*a2
        c = c1*d2 + a1*c2
        d = d1*d2 + b1*c2
        e = e1 + c1*f2 + a1*e2
        f = f1 + d1*f2 + b1*e2

        return Transform(a, b, c, d, e, f)

    def __str__(self):
        a, b, c, d, e, f = self.mat
        return f'Transform({a=:.3f} {b=:.3f} {c=:.3f} {d=:.3f} {e=:.3f} {f=:.3f})'

    def transform_point(self, x, y):
        a, b, c, d, e, f = self.mat
        x_new = a*x + c*y + e
        y_new = b*x + d*y + f
        return x_new, y_new

    @classmethod
    def translate(kls, x, y):
        return kls(1, 0, 0, 1, x, y)

    @classmethod
    def scale(kls, x, y):
        return kls(x, 0, 0, y, 0, 0)

    @classmethod
    def rotate(kls, a, x=0, y=0):
        s, c = math.sin(a), math.cos(a)
        mat = kls(c, s, -s, c, 0, 0)
        if not math.isclose(x, 0) or not math.isclose(y, 0):
            mat = kls.translate(x, y) * (mat * kls.translate(-x, -y))
        return mat

    @classmethod
    def skew_x(kls, a):
        return kls(1, 0, math.tan(a), 1, 0, 0)

    @classmethod
    def skew_y(kls, a):
        return kls(1, math.tan(a), 0, 1, 0, 0)

    @classmethod
    def _parse_single_svg(kls, xform_string):
        _transform, name, nums, _garbage = re.match(kls.xform_re, xform_string).groups()
        nums = [float(x) for x in nums.strip().split()]
        match (name, *nums):
            case ('matrix', a, b, c, d, e, f):
                return kls(a, b, c, d, e, f)
            case ('translate', x):
                return kls.translate(x, 0)
            case ('translate', x, y):
                return kls.translate(x, y)
            case ('scale', s):
                return kls.scale(s, s)
            case ('scale', x, y):
                return kls.scale(x, y)
            case ('rotate', a):
                return kls.rotate(math.radians(a))
            case ('rotate', a, x, y):
                return kls.rotate(math.radians(a), x, y)
            case ('skewX', a):
                return kls.skew_x(math.radians(a))
            case ('skewY', a):
                return kls.skew_y(math.radians(a))

    @classmethod
    def parse_svg(kls, xform_string):
        mat = kls()
        for xf in re.finditer(kls.xform_re, xform_string):
            component, command, params, garbage = xf.groups()
            if garbage:
                raise ValueError(f'Unknown SVG transform {garbage!r}')
            mat *= kls._parse_single_svg(xf.group(0))
        return mat

    def as_svg(self):
        a, b, c, d, e, f = self.mat
        return f'matrix({a} {b} {c} {d} {e} {f})'


def parse_path_d(d):
    # Reference: https://developer.mozilla.org/en-US/docs/Web/SVG/Attribute/d#path_commands
    cur_x, cur_y = None, None
    start_x, start_y = None, None
    for m in re.finditer(r'([MmLlHhVvCcSsQqTtAaZz])\s*((-?[0-9.]+)(\s*[\s,]\s*-?[0-9.]+)*)', d):
        command = m.group(1)
        is_relative, command = command.islower(), command.upper()
        params = [float(x or 0) for x in re.split(r'\s*[\s,]\s*', m.group(2).strip())]

        def r(x, y, reset=True):
            if is_relative:
                x, y = x+cur_x, y+cur_y
            if reset:
                cur_x, cur_y = x, y
            return x, y

        if command == 'Z':
            if params:
                raise ValueError('Z (close path) command followed by numeric parameters')
            if not math.isclose(cur_x, start_x) or not math.isclose(cur_y, start_y):
                yield 'L', (start_x, start_y)

        else:
            while params:
                match (command, *params):
                    case ('M', x, y, *_extra):
                        yield 'M', r(x, y)
                        start_x, start_y = cur_x, cur_y
                        command = 'L'
                        params = params[2:]
                    case ('L', x, y, *_extra):
                        yield 'L', r(x, y)
                        params = params[2:]
                    case ('H', x, *_extra):
                        yield 'L', r(x, 0 if is_relative else cur_y)
                        params = params[1:]
                    case ('V', y, *_extra):
                        yield 'L', r(0 if is_relative else cur_x, y)
                        params = params[1:]
                    case ('C', x1, y1, x2, y2, x, y, *_extra):
                        yield 'C', r(x1, y1, False), r(x2, y2, False), r(x, y)
                        params = params[6:]
                    case ('S', dx2, dy2, x, y, *_extra):
                        yield 'S', r(dx2, dy2, False), r(x, y)
                        params = params[4:]
                    case ('Q', x1, y1, x, y, *_extra):
                        yield 'Q', r(x1, y1, False), r(x, y)
                        params = params[4:]
                    case ('T', x, y, *_extra):
                        yield 'T', r(x, y)
                        params = params[2:]
                    case ('A', rx, ry, a, l, s, x, y, *_extra):
                        yield 'A', (rx, ry), a, l, s, r(x, y)
                        params = params[7:]
            

