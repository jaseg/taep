#!/usr/bin/env python3

import tempfile
import shutil
import re
import base64
import copy
import subprocess
import shlex
import os
import sys
import math
from pathlib import Path

import click
from bs4 import BeautifulSoup

from svg_util import *


def run_cargo_command(binary, *args, **kwargs):
    # By default, try a number of options:
    candidates = [
        # somewhere in $PATH
        binary,
        # wasi-wrapper in $PATH
        f'wasi-{binary}',
        # in user-local cargo installation
        Path.home() / '.cargo' / 'bin' / binary,
        # wasi-wrapper in user-local pip installation
        Path.home() / '.local' / 'bin' / f'wasi-{binary}',
        # next to our current python interpreter (e.g. in virtualenv)
        str(Path(sys.executable).parent / f'wasi-{binary}')
        ]

    return run_command(binary, *args, candidates=candidates, **kwargs)


def run_command(binary, *args, candidates=[], **kwargs):
    cmd_args = []
    for key, value in kwargs.items():
        if value is not None:
            if value is False:
                continue

            if len(key) > 1:
                cmd_args.append(f'--{key.replace("_", "-")}')
            else:
                cmd_args.append(f'-{key}')

            if value is not True:
                cmd_args.append(str(value))
    cmd_args.extend(map(str, args))

    # By default, try a number of options:
    if not candidates:
        candidates = [binary]

    # if envvar is set, try that first.
    if (env_var := os.environ.get(binary.upper())):
        candidates = [str(Path(env_var).expanduser()), *candidates]

    for cand in candidates:
        try:
            res = subprocess.run([cand, *cmd_args], check=True)
            break
        except FileNotFoundError:
            continue
    else:
        raise SystemError(f'{binary} executable not found')


@click.group()
def cli():
    pass

@cli.command()
@click.option('--num-rows', type=int, default=5, help='Number of tapes')
@click.option('--tape-width', type=float, default=24, help='Width of tape')
@click.option('--tape-border', type=float, default=3, help='Width of empty border at the edges of the tape in mm')
@click.option('--tape-spacing', type=float, default=2, help='Space between tapes')
@click.option('--tape-length', type=float, default=250, help='Length of tape segments')
@click.option('--magic-color', type=str, default='#cc0000', help='SVG color of tape')
@click.argument('output_svg', type=click.File(mode='w'), default='-')
def template(num_rows, tape_width, tape_border, tape_spacing, tape_length, magic_color, output_svg):
    pitch = tape_width + tape_spacing
    tags = [Tag('g', inkscape__layer='Layer 1', inkscape__groupmode='layer', id='layer1', children=[
        Tag('g', id='g1', children=[
            Tag('g', id=f'tape{i}', children=[
                Tag('path', id=f'tape{i}_outline', fill='none', stroke='black', opacity='0.3', stroke_width=f'{tape_width}px',
                    d=f'M 0 {tape_width/2 + i*pitch} {tape_length} {tape_width/2 + i*pitch}'),
                Tag('path', id=f'tape{i}_printable_area', fill='none', stroke=magic_color, stroke_width=f'{tape_width-2*tape_border}px',
                    d=f'M 0 {tape_width/2 + i*pitch} {tape_length} {tape_width/2 + i*pitch}'),
                ])
            for i in range(num_rows)
            ])
        ])]

    bounds = (0, 0), (tape_length, num_rows*tape_width + (num_rows-1)*tape_spacing)
    svg = setup_svg(tags, bounds, margin=tape_width, inkscape=True)
    output_svg.write(str(svg))

@cli.command()
@click.option('--magic-color', type=str, default='#cc0000', help='SVG color of tape')
@click.option('--dpi', type=float, default=180, help='Printer bitmap resolution in DPI')
@click.option('--pixel-height', type=int, default=127, help='Printer tape vertical pixel height')
@click.argument('input_svg', type=click.File(mode='r'), default='-')
def dither(input_svg, magic_color, dpi, pixel_height):
    tmpdir = Path('/tmp/foo') # FIXME debug
    with tempfile.NamedTemporaryFile('w', suffix='.svg') as tmp_in_svg,\
            tempfile.NamedTemporaryFile('r', suffix='.svg') as tmp_out_svg:
        tmp_in_svg.write(input_svg.read())
        tmp_in_svg.flush()

        try:
            run_cargo_command('usvg', *shlex.split(os.environ.get('USVG_OPTIONS', '')), tmp_in_svg.name, tmp_out_svg.name)
        except SystemError:
            raise ClickException('Cannot find usvg. Please install usvg using cargo, or pass the full path to the usvg binary in the USVG environment variable.')

        soup = BeautifulSoup(tmp_out_svg.read(), 'xml')

    preview_images = []
    tape_num = 1
    for i, path in enumerate(list(soup.find_all('path'))):
        if path.get('stroke').lower() != magic_color:
            continue
        path_id = path.get('id', '<no id?>')

        commands = list(parse_path_d(path.get('d', '')))
        if len(commands) != 2:
            print('Path', path_id, 'has magic color, but has more than two nodes. Ignoring.', file=sys.stderr)
            continue
        if commands[1][0] != 'L':
            print('Path', path_id, 'has magic color, but has a curve. Ignoring.', file=sys.stderr)
            continue
        if commands[0][0] != 'M':
            print('Path', path_id, 'has magic color, but is malformed (does not start with M command). Ignoring.', file=sys.stderr)
            continue
        (_c1, (x1, y1)), (_c2, (x2, y2)) = commands

        mat = Transform.parse_svg(path.get('transform', ''))
        for parent in path.parents:
            xf = Transform.parse_svg(parent.get('transform', ''))
            mat = xf * mat  # make sure we apply the parent transform from the left, i.e. after the child transform
        x1, y1 = mat.transform_point(x1, y1)
        x2, y2 = mat.transform_point(x2, y2)
        path_len = math.dist((x1, y1), (x2, y2))

        if math.isclose(path_len, 0, abs_tol=1e-3):
            print('Path', path_id, 'has magic color, but has (almost) zero length. Ignoring.', file=sys.stderr)
            continue

        if not (stroke_w := path.get('stroke-width')):
            print('Path', path_id, 'has magic color, but has no defined stroke width. Ignoring.', file=sys.stderr)
            continue
        stroke_w = float(re.match('[-0-9.]+', stroke_w).group(0))

        path_angle = math.atan2((y2-y1), (x2-x1))
        dx, dy = (x2-x1)/path_len, (y2-y1)/path_len

        sx1, sy1 = mat.transform_point(x1-dy*stroke_w/2, y1+dx*stroke_w/2)
        sx2, sy2 = mat.transform_point(x1+dy*stroke_w/2, y1-dx*stroke_w/2)
        stroke_w = round(math.dist((sx1, sy1), (sx2, sy2)), 3)

        out_soup = copy.copy(soup)

        print(f'found path {path_id} of length {path_len:2f} and angle {math.degrees(path_angle):.1f} deg with physical stroke width {stroke_w:.2f} from ({x1:.2f}, {y1:.2f}) to ({x2:.2f}, {y2:.2f})', file=sys.stderr)
        #out_soup.find('svg').append(out_soup.new_tag('path', fill='none', stroke='blue', stroke_width=f'24px',
        #            d=f'M {x1} {y1} L {x2} {y2}'))
        xf = Transform.translate(0, stroke_w/2) * Transform.rotate(-path_angle) * Transform.translate(-x1, -y1)
        g = out_soup.new_tag('g', id='transform-group', transform=xf.as_svg())
        k = list(out_soup.find('svg').contents)
        for c in k:
            g.append(c.extract())
        out_soup.find('svg').append(g)
        out_soup.find('path', id=path['id']).parent.decompose()
        out_soup.find('svg')['viewBox'] = f'0 0 {path_len} {stroke_w}'
        out_soup.find('svg')['width'] = f'{path_len}mm'
        out_soup.find('svg')['height'] = f'{stroke_w}mm'

        with tempfile.NamedTemporaryFile('w', suffix='.svg') as tmp_svg,\
                tempfile.NamedTemporaryFile('rb', suffix='.png') as tmp_png,\
                tempfile.NamedTemporaryFile('rb', suffix='.png') as tmp_dither:
            tmp_svg.write(out_soup.prettify())
            tmp_svg.flush()
            run_cargo_command('resvg', tmp_svg.name, tmp_png.name, width=round(Inch(path_len, 'mm')*dpi), height=pixel_height)
            shutil.copy(tmp_png.name, f'/tmp/debug_{i}.png')

            run_command('didder', 'edm', '--serpentine', 'FloydSteinberg', palette='black white', i=tmp_png.name, o=tmp_dither.name)
            shutil.copy(tmp_dither.name, f'/tmp/dither_{tape_num}.png')
            preview_images.append(Tag('image', width=path_len, height=stroke_w, preserveAspectRatio='none',
                                      id=f'preview_image_{tape_num}',
                                      x=0, y=0,
                                      transform=f'translate({x1} {y1}) rotate({math.degrees(path_angle)}) translate(0 {-stroke_w/2})',
                                      xlink__href=f'data:image/png;base64,{base64.b64encode(tmp_dither.read()).decode()}'))

            print('wrote', f'/tmp/dither_{tape_num}.png')
            tape_num += 1

    tags = [Tag('g', inkscape__layer='Layer 1', inkscape__groupmode='layer', id='layer1', children=[
            Tag('g', id='g1', children=preview_images)
        ])]

    vbx, vby, vbw, vbh = map(float, soup.find('svg')['viewBox'].split())
    bounds = (vbx, vby), (vbx+vbw, vby+vbh)
    svg = setup_svg(tags, bounds, inkscape=True)
    Path('/tmp/preview.svg').write_text(str(svg))


if __name__ == '__main__':
    cli()
