#!/usr/bin/env python3

import tempfile
import webbrowser
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

from .svg_util import *


__version__ = "v1.0.0-rc1"


USVG_DPI = 96.0

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
    if (env_var := os.environ.get(Path(binary).name.replace('-', '_').upper())):
        candidates = [str(Path(env_var).expanduser()), *candidates]

    for cand in candidates:
        try:
            res = subprocess.run([cand, *cmd_args], check=True)
            break
        except FileNotFoundError:
            continue
    else:
        raise SystemError(f'{binary} executable not found')


def simplify_and_open_svg(data):
    with tempfile.NamedTemporaryFile('w', suffix='.svg') as tmp_in_svg,\
            tempfile.NamedTemporaryFile('r', suffix='.svg') as tmp_out_svg:
        tmp_in_svg.write(data)
        tmp_in_svg.flush()

        try:
            run_cargo_command('usvg', *shlex.split(os.environ.get('USVG_OPTIONS', '')), tmp_in_svg.name, tmp_out_svg.name)
        except SystemError:
            raise click.ClickException('Cannot find usvg. Please install usvg using cargo, or pass the full path to the usvg binary in the USVG environment variable.')
        except subprocess.CalledProcessError as e:
            raise click.ClickException(f'usvg exited with return code {e.returncode}.')

        return BeautifulSoup(tmp_out_svg.read(), 'xml')


def print_tape(png_file):
    try:
        run_command('ptouch-print', '--image', png_file)
    except SystemError:
        raise click.ClickException('Cannot find ptouch-print. Please install ptouch-print from the upstream repo at https://git.familie-radermacher.ch/linux/ptouch-print.git . You can pass the full path to the ptouch-print binary in the PTOUCH_PRINT environment variable if it\'s not in $PATH.')
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f'ptouch-print exited with return code {e.returncode}.')


def calc_scale(soup):
    svg = soup.find('svg')
    vb_x, vb_y, vb_w, vb_h = map(float, svg['viewBox'].split())
    doc_w, doc_h = float(svg['width']), float(svg['height'])
    doc_w_mm = doc_w / USVG_DPI * 25.4
    doc_h_mm = doc_h / USVG_DPI * 25.4
    mm_per_px_x = doc_w_mm / vb_w
    mm_per_px_y = doc_h_mm / vb_h
    return mm_per_px_x, mm_per_px_y


def do_dither(soup, magic_color, dpi, pixel_height):
    mm_per_px_x, mm_per_px_y = calc_scale(soup)

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
        path_len_mm = math.dist((x1*mm_per_px_x, y1*mm_per_px_y), (x2*mm_per_px_x, y2*mm_per_px_y))

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
        stroke_w_mm = round(math.dist((sx1*mm_per_px_x, sy1*mm_per_px_y), (sx2*mm_per_px_x, sy2*mm_per_px_y)), 3)

        out_soup = copy.copy(soup)

        print(f'Identified tape from path "{path_id}", length {path_len_mm:2f} mm, angle {math.degrees(path_angle):.1f} deg with physical stroke width {stroke_w_mm:.2f} mm from ({x1:.2f}, {y1:.2f}) to ({x2:.2f}, {y2:.2f})')
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
        out_soup.find('svg')['width'] = f'{path_len_mm}mm'
        out_soup.find('svg')['height'] = f'{stroke_w_mm}mm'

        with tempfile.NamedTemporaryFile('w', suffix='.svg') as tmp_svg,\
                tempfile.NamedTemporaryFile('rb', suffix='.png') as tmp_png,\
                tempfile.NamedTemporaryFile('rb', suffix='.png') as tmp_dither:
            tmp_svg.write(out_soup.prettify())
            tmp_svg.flush()
            run_cargo_command('resvg', tmp_svg.name, tmp_png.name, width=round(Inch(path_len, 'mm')*dpi), height=pixel_height)

            args = shlex.split(os.environ.get('DIDDER_ARGS', 'edm --serpentine FloydSteinberg'))
            run_command('didder', *args, palette='black white', i=tmp_png.name, o=tmp_dither.name)
            yield (x1, y1, path_angle, stroke_w, path_len), tmp_dither.read()


def make_preview(input_svg, out_file, *dither_args, assembly_labels=False, **dither_kwargs):
    imgs = []
    labels = []
    soup = simplify_and_open_svg(input_svg)

    for tape_num, ((x1, y1, path_angle, stroke_w, path_len), img) in enumerate(do_dither(soup, *dither_args, **dither_kwargs), start=1):
        xf = f'translate({x1} {y1}) rotate({math.degrees(path_angle)}) translate(0 {-stroke_w/2})'
        imgs.append(Tag('image', width=path_len, height=stroke_w, preserveAspectRatio='none',
                        id=f'preview_image_{tape_num}',
                        x=0, y=0,
                        transform=xf,
                        xlink__href=f'data:image/png;base64,{base64.b64encode(img).decode()}'))
        labels.append(Tag('path', fill='none', stroke_width='0.2px', stroke='red', transform=xf,
                          d=f'M 0 0 h {path_len} v {stroke_w} h {-path_len} Z'))
        labels.append(Tag('text', fill='red', stroke='none', font_size=f'{stroke_w*0.8}px', transform=xf,
                          x='2px', y=f'{stroke_w*0.9}px', children=[f'{tape_num}']))

    layer = Tag('g', inkscape__layer='Preview', inkscape__groupmode='layer', id='layer_preview', children=[
            Tag('g', id='preview_images', children=imgs),
        ])

    if assembly_labels:
        layer.children.append(Tag('g', id='assembly_instructions', children=labels))

    vbx, vby, vbw, vbh = map(float, soup.find('svg')['viewBox'].split())
    bounds = (vbx, vby), (vbx+vbw, vby+vbh)
    svg = setup_svg([layer], bounds, inkscape=True)

    if out_file is not None:
        out_file.write(str(svg))
    else:
        with tempfile.NamedTemporaryFile(suffix='.svg', mode='w', delete=False) as f:
            f.write(str(svg))
            f.flush()
            webbrowser.open_new_tab(f'file://{f.name}')


@click.group()
def cli():
    pass

@cli.command()
@click.option('--num-rows', type=int, default=5, help='Number of tapes')
@click.option('--tape-width', type=float, default=24, help='Width of tape')
@click.option('--tape-border', type=float, default=3, help='Width of empty border at the edges of the tape in mm')
@click.option('--tape-spacing', type=float, default=2, help='Space between tapes')
@click.option('--tape-length', type=float, default=250, help='Length of tape segments')
@click.option('--magic-color', type=str, default='#cc0301', help='SVG color of tape')
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


@cli.command('print')
@click.option('--magic-color', type=str, default='#cc0301', help='SVG color of tape')
@click.option('--dpi', type=float, default=180, help='Printer bitmap resolution in DPI')
@click.option('--pixel-height', type=int, default=127, help='Printer tape vertical pixel height')
@click.option('--confirm/--no-confirm', default=True, help='Ask for confirmation before printing each tape')
@click.option('--tape', type=str, default='-', help='The index numbers of which tapes to print. Comma-separate list, each entry is either a single number or a "3-5" style range where both ends are included.')
@click.argument('input_svg', type=click.File(mode='r'), default='-')
def cli_print(input_svg, tape, magic_color, dpi, pixel_height, confirm):
    with tempfile.TemporaryDirectory() as tmpdir:
        out = {}

        soup = simplify_and_open_svg(input_svg.read())
        for i, (_tape_pos, img) in enumerate(do_dither(soup, magic_color=magic_color, dpi=dpi, pixel_height=pixel_height), start=1):
            f = Path(tmpdir) / f'dither_tape_{i}.png'
            f.write_bytes(img)
            out[i] = f

        selected = set()
        for entry in tape.split(','):
            start, sep, stop = entry.partition('-')
            if not sep:
                selected.add(int(start))
            else:
                start = int(start) if start else min(out)
                stop = int(stop) if stop else max(out)
                selected |= set(range(start, stop+1))

        for tape in sorted(selected):
            if confirm:
                if not click.confirm(f'Do you want to continue and print tape {tape}?'):
                    break
            print_tape(out[tape])


@cli.command()
@click.option('--magic-color', type=str, default='#cc0301', help='SVG color of tape')
@click.option('--dpi', type=float, default=180, help='Printer bitmap resolution in DPI')
@click.option('--pixel-height', type=int, default=127, help='Printer tape vertical pixel height')
@click.argument('input_svg', type=click.File(mode='r'), default='-')
@click.argument('output_svg', type=click.File(mode='w'), required=False)
def preview(input_svg, output_svg, magic_color, dpi, pixel_height):
    make_preview(input_svg.read(), output_svg, magic_color=magic_color, dpi=dpi, pixel_height=pixel_height, assembly_labels=False)


@cli.command()
@click.option('--magic-color', type=str, default='#cc0301', help='SVG color of tape')
@click.option('--dpi', type=float, default=180, help='Printer bitmap resolution in DPI')
@click.option('--pixel-height', type=int, default=127, help='Printer tape vertical pixel height')
@click.argument('input_svg', type=click.File(mode='r'), default='-')
@click.argument('output_svg', type=click.File(mode='w'), required=False)
def assembly(input_svg, output_svg, magic_color, dpi, pixel_height):
    make_preview(input_svg.read(), output_svg, magic_color=magic_color, dpi=dpi, pixel_height=pixel_height, assembly_labels=True)


@cli.command()
@click.option('--magic-color', type=str, default='#cc0301', help='SVG color of tape')
@click.option('--dpi', type=float, default=180, help='Printer bitmap resolution in DPI')
@click.option('--pixel-height', type=int, default=127, help='Printer tape vertical pixel height')
@click.argument('input_svg', type=click.File(mode='r'), default='-')
@click.argument('output_dir', type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
def dither(input_svg, output_dir, magic_color, dpi, pixel_height):
    output_dir.mkdir(exist_ok=True)
    soup = simplify_and_open_svg(input_svg.read())
    for i, (_tape_pos, img) in enumerate(do_dither(soup, magic_color=magic_color, dpi=dpi, pixel_height=pixel_height), start=1):
        outfile = output_dir / f'dither_tape_{i}.png'
        outfile.write_bytes(img)
        print(f'Wrote {outfile}')


if __name__ == '__main__':
    cli()

